import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from ..config import (
    DOWNLOAD_CACHE_DIR,
    DOWNLOAD_CACHE_MAX_BYTES,
    DOWNLOAD_CACHE_MIN_FREE_BYTES,
    DOWNLOAD_CACHE_RETENTION_HOURS,
)

_NO_COMPRESS_SUFFIXES = {
    ".zip", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
    ".mp3", ".wav", ".aac", ".m4a", ".ogg", ".jpg", ".jpeg",
    ".png", ".webp", ".gif", ".pdf", ".7z", ".rar",
}

_DOWNLOAD_CACHE_WRITE_LOCK = threading.Lock()


def _safe_zip_prefix(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "download")).strip("._")
    return (text or "download")[:96]


def _zip_compression_for(path: Path) -> int:
    return zipfile.ZIP_STORED if path.suffix.lower() in _NO_COMPRESS_SUFFIXES else zipfile.ZIP_DEFLATED


def _file_signature(path: Path, root: Path) -> dict:
    stat = path.stat()
    return {
        "rel": path.relative_to(root).as_posix(),
        "size": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    }


def _cache_digest(*, source_root: Path, files: list[Path], filename_prefix: str) -> str:
    payload = {
        "prefix": filename_prefix,
        "root": str(source_root.resolve()),
        "files": [_file_signature(item, source_root.resolve()) for item in files],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _write_zip_atomic(zip_path: Path, *, root: Path, files: list[Path]) -> None:
    tmp_path = zip_path.with_suffix(zip_path.suffix + f".tmp.{os.getpid()}")
    try:
        if tmp_path.exists():
            tmp_path.unlink()
        with zipfile.ZipFile(tmp_path, "w", allowZip64=True) as archive:
            for item in files:
                archive.write(
                    item,
                    arcname=item.relative_to(root),
                    compress_type=_zip_compression_for(item),
                )
        tmp_path.replace(zip_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise


def _wait_for_zip_lock(lock_path: Path, *, timeout_sec: float = 7200.0) -> bool:
    deadline = time.time() + max(1.0, timeout_sec)
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\ntime={time.time()}\n")
            return True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > timeout_sec:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            time.sleep(0.25)
    return False


def _valid_selected_files(source_root: Path, files: list[Path]) -> list[Path]:
    root_resolved = source_root.resolve()
    valid_files: list[Path] = []
    seen: set[str] = set()

    for item in files:
        try:
            resolved = Path(item).resolve()
        except Exception:
            continue
        if resolved == root_resolved or root_resolved not in resolved.parents:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        valid_files.append(resolved)
    return sorted(valid_files, key=lambda path: path.relative_to(root_resolved).as_posix())


def _required_zip_bytes(files: list[Path]) -> int:
    # Media files use ZIP_STORED, so source bytes are a safe upper bound.
    return sum(max(0, int(item.stat().st_size)) for item in files) + (len(files) * 512)


def ensure_download_cache_capacity(*, required_bytes: int, protected_paths: set[Path] | None = None) -> dict[str, int]:
    """Reserve disk space for a new ZIP without violating the cache budget."""
    from fastapi import HTTPException

    result = cleanup_download_cache(
        retention_hours=DOWNLOAD_CACHE_RETENTION_HOURS,
        max_bytes=DOWNLOAD_CACHE_MAX_BYTES,
        min_free_bytes=DOWNLOAD_CACHE_MIN_FREE_BYTES,
        required_bytes=max(0, int(required_bytes or 0)),
        protected_paths=protected_paths,
    )
    if not result["capacity_ok"]:
        raise HTTPException(
            status_code=507,
            detail={
                "code": "download_cache_insufficient_space",
                "required_bytes": result["required_bytes"],
                "cache_bytes": result["cache_bytes_after"],
                "free_bytes": result["free_bytes_after"],
                "min_free_bytes": result["min_free_bytes"],
                "max_cache_bytes": result["max_bytes"],
            },
        )
    return result


def zip_named_files(*, entries: list[tuple[Path, str]], filename_prefix: str) -> Path:
    """Create one capacity-guarded ZIP from files that need explicit archive names."""
    from fastapi import HTTPException

    valid_entries: list[tuple[Path, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, archive_name in entries:
        try:
            resolved = Path(source).resolve()
        except Exception:
            continue
        normalized_name = str(archive_name or "").replace("\\", "/").lstrip("/")
        if not resolved.exists() or not resolved.is_file() or not normalized_name:
            continue
        if ".." in Path(normalized_name).parts:
            continue
        key = (str(resolved), normalized_name)
        if key in seen:
            continue
        seen.add(key)
        valid_entries.append((resolved, normalized_name))
    if not valid_entries:
        raise HTTPException(status_code=404, detail="No valid files found for selected items")

    required_bytes = _required_zip_bytes([source for source, _name in valid_entries])
    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_prefix = _safe_zip_prefix(filename_prefix)
    with _DOWNLOAD_CACHE_WRITE_LOCK:
        ensure_download_cache_capacity(required_bytes=required_bytes)
        with tempfile.NamedTemporaryFile(
            prefix=f"{safe_prefix}_",
            suffix=".zip.tmp",
            dir=str(DOWNLOAD_CACHE_DIR),
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        zip_path = Path(str(tmp_path)[:-4])
        try:
            ensure_download_cache_capacity(required_bytes=required_bytes, protected_paths={tmp_path})
            with zipfile.ZipFile(tmp_path, "w", allowZip64=True) as archive:
                for source, archive_name in valid_entries:
                    archive.write(source, arcname=archive_name, compress_type=_zip_compression_for(source))
            tmp_path.replace(zip_path)
            return zip_path
        except Exception:
            for path in (tmp_path, zip_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
            raise


def cached_zip_selected_files(source_root: Path, *, files: list[Path], filename_prefix: str) -> Path:
    """Create or reuse a deterministic ZIP for resumable large downloads."""
    from fastapi import HTTPException

    if not source_root.exists() or not source_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {source_root}")
    if not files:
        raise HTTPException(status_code=400, detail="No files selected")

    root_resolved = source_root.resolve()
    valid_files = _valid_selected_files(source_root, files)
    if not valid_files:
        raise HTTPException(status_code=404, detail="No valid files found for selected items")

    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_prefix = _safe_zip_prefix(filename_prefix)
    digest = _cache_digest(source_root=root_resolved, files=valid_files, filename_prefix=safe_prefix)
    zip_path = DOWNLOAD_CACHE_DIR / f"{safe_prefix}_{digest}.zip"
    lock_path = DOWNLOAD_CACHE_DIR / f"{safe_prefix}_{digest}.lock"

    if zip_path.exists() and zip_path.stat().st_size > 0:
        return zip_path

    required_bytes = _required_zip_bytes(valid_files)
    ensure_download_cache_capacity(required_bytes=required_bytes)
    if not _wait_for_zip_lock(lock_path):
        raise HTTPException(status_code=503, detail="download_zip_busy")
    try:
        if zip_path.exists() and zip_path.stat().st_size > 0:
            return zip_path
        ensure_download_cache_capacity(
            required_bytes=required_bytes,
            protected_paths={lock_path},
        )
        _write_zip_atomic(zip_path, root=root_resolved, files=valid_files)
        return zip_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def zip_directory(source_dir: Path, *, filename_prefix: str) -> Path:
    """Create a zip archive of a directory."""
    from fastapi import HTTPException
    if not source_dir.exists() or not source_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {source_dir}")
    source_resolved = source_dir.resolve()
    source_files = [item for item in sorted(source_dir.rglob("*")) if item.is_file()]
    if not source_files:
        raise HTTPException(status_code=404, detail=f"No files available in: {source_dir.name}")
    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_download_cache_capacity(required_bytes=_required_zip_bytes(source_files))
    with tempfile.NamedTemporaryFile(
        prefix=f"{filename_prefix}_",
        suffix=".zip",
        dir=str(DOWNLOAD_CACHE_DIR),
        delete=False,
    ) as handle:
        zip_path = Path(handle.name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in source_files:
            item_resolved = item.resolve()
            if source_resolved not in item_resolved.parents and item_resolved != source_resolved:
                continue
            archive.write(item, arcname=item.relative_to(source_dir))
    return zip_path


def zip_selected_files(source_root: Path, *, files: list[Path], filename_prefix: str) -> Path:
    """Create a zip archive from an explicit list of files under source_root."""
    from fastapi import HTTPException

    if not source_root.exists() or not source_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {source_root}")
    if not files:
        raise HTTPException(status_code=400, detail="No files selected")

    root_resolved = source_root.resolve()
    valid_files: list[Path] = []
    seen: set[str] = set()

    for item in files:
        try:
            resolved = Path(item).resolve()
        except Exception:
            continue
        if resolved == root_resolved or root_resolved not in resolved.parents:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        valid_files.append(resolved)

    if not valid_files:
        raise HTTPException(status_code=404, detail="No valid files found for selected items")

    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_download_cache_capacity(required_bytes=_required_zip_bytes(valid_files))
    with tempfile.NamedTemporaryFile(
        prefix=f"{filename_prefix}_",
        suffix=".zip",
        dir=str(DOWNLOAD_CACHE_DIR),
        delete=False,
    ) as handle:
        zip_path = Path(handle.name)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in valid_files:
            archive.write(item, arcname=item.relative_to(root_resolved))

    return zip_path


def cleanup_download_cache(
    *,
    retention_hours: float = 24.0,
    batch_limit: int = 500,
    max_bytes: int = DOWNLOAD_CACHE_MAX_BYTES,
    min_free_bytes: int = DOWNLOAD_CACHE_MIN_FREE_BYTES,
    required_bytes: int = 0,
    protected_paths: set[Path] | None = None,
) -> dict[str, int]:
    """Delete expired/oldest ZIPs until retention, cache-size, and free-space gates pass."""
    retention = max(0.0, float(retention_hours or 0.0))
    limit = max(1, int(batch_limit or 1))
    max_limit = max(0, int(max_bytes or 0))
    reserve = max(0, int(min_free_bytes or 0))
    required = max(0, int(required_bytes or 0))
    protected = {Path(item).resolve() for item in (protected_paths or set())}
    result = {
        "enabled": int(retention > 0 or max_limit > 0 or reserve > 0),
        "files_scanned": 0,
        "files_deleted": 0,
        "bytes_deleted": 0,
        "errors": 0,
        "required_bytes": required,
        "max_bytes": max_limit,
        "min_free_bytes": reserve,
        "cache_bytes_after": 0,
        "free_bytes_after": 0,
        "capacity_ok": 1,
    }
    if not DOWNLOAD_CACHE_DIR.exists():
        DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        result["free_bytes_after"] = int(shutil.disk_usage(DOWNLOAD_CACHE_DIR).free)
        result["capacity_ok"] = int(result["free_bytes_after"] >= required + reserve)
        return result

    cutoff = time.time() - (retention * 3600.0)
    items = sorted(DOWNLOAD_CACHE_DIR.iterdir(), key=lambda path: path.stat().st_mtime if path.exists() else 0)
    for item in items:
        if result["files_scanned"] >= limit:
            break
        try:
            if not item.is_file():
                continue
            if item.resolve() in protected:
                continue
            # The cache directory is dedicated to generated downloads; keep the cleanup file-only.
            stat = item.stat()
            result["files_scanned"] += 1
            if retention <= 0 or stat.st_mtime > cutoff:
                continue
            size = int(stat.st_size or 0)
            item.unlink()
            result["files_deleted"] += 1
            result["bytes_deleted"] += size
        except Exception:
            result["errors"] += 1

    files = []
    cache_bytes = 0
    for item in DOWNLOAD_CACHE_DIR.iterdir():
        try:
            if not item.is_file():
                continue
            stat = item.stat()
            cache_bytes += max(0, int(stat.st_size or 0))
            if item.resolve() not in protected and item.suffix.lower() == ".zip":
                files.append((stat.st_mtime, item, max(0, int(stat.st_size or 0))))
        except Exception:
            result["errors"] += 1
    files.sort(key=lambda row: row[0])

    free_bytes = int(shutil.disk_usage(DOWNLOAD_CACHE_DIR).free)
    effective_max = max(max_limit, required) if max_limit > 0 else 0

    def over_budget() -> bool:
        cache_over = effective_max > 0 and (cache_bytes + required) > effective_max
        free_short = free_bytes < (required + reserve)
        return cache_over or free_short

    for _mtime, item, size in files:
        if not over_budget() or result["files_deleted"] >= limit:
            break
        try:
            item.unlink()
            cache_bytes = max(0, cache_bytes - size)
            free_bytes += size
            result["files_deleted"] += 1
            result["bytes_deleted"] += size
        except FileNotFoundError:
            cache_bytes = max(0, cache_bytes - size)
        except Exception:
            result["errors"] += 1

    result["cache_bytes_after"] = cache_bytes
    result["free_bytes_after"] = free_bytes
    result["capacity_ok"] = int(not over_budget())
    return result

