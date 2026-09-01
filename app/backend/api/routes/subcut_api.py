"""Focused API for SJ88 SubCut Studio.

This router intentionally depends only on the shared authentication/database
layer and the two SubCut processing modes. It avoids importing the legacy
full-editor backend so the standalone package can run even when optional old
editor modules are not installed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ._auth_guard import require_request_user, user_id_from_user
from ...config import CHUNK_SIZE, LEGACY_UI_JOBS_DIR, USER_WORKSPACE_ROOT
from ...helpers.zip import cached_zip_selected_files
from ...services.constants import VIDEO_EXTS
from ...services.job_store import JobStore
from ...services.legacy import job_to_legacy


router = APIRouter(prefix="", tags=["SubCut"])

_SUBCUT_MODES = {"autosu_only", "silence_trim_only"}
_INPUT_MANIFEST = ".autosu_only_inputs.json"
_UPLOAD_SESSION = ".subcut_upload_session.json"
_CHUNK_DIR = ".subcut_chunks"
_WORK_DIRS = {".silence_trim_work", ".subcut_chunks"}
_TERMINAL_STATUSES = {"done", "failed", "error", "cancelled"}
_ALLOWED_SETTINGS = {
    "workflow",
    "trim_silence",
    "trim_silence_preset",
    "trim_silence_threshold_db",
    "trim_silence_min_silence_sec",
    "trim_silence_margin_sec",
    "trim_silence_min_keep_sec",
    "trim_silence_min_output_sec",
    "subtitle_template_id",
    "subtitle_language",
    "subtitle_position_percent",
    "subtitle_font_size",
    "subtitle_font_color",
    "subtitle_border_color",
    "subtitle_background_color",
    "subtitle_background_opacity",
    "subtitle_max_words_per_line",
    "subtitle_max_syllables_per_line",
    "source_app",
}
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_LOCKS_GUARD = threading.Lock()
_UPLOAD_LOCKS: dict[str, threading.RLock] = {}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


_MAX_FILES = _env_int("APP_SUBCUT_MAX_FILES", 100, minimum=1, maximum=1000)
_MAX_FILE_BYTES = _env_int(
    "APP_SUBCUT_MAX_FILE_BYTES",
    20 * 1024 * 1024 * 1024,
    minimum=1024,
    maximum=2 * 1024 * 1024 * 1024 * 1024,
)
_MAX_CHUNKS = max(1, math.ceil(_MAX_FILE_BYTES / CHUNK_SIZE))


def _get_store() -> JobStore:
    from ...config import DB_PATH

    return JobStore(DB_PATH)


def _job_lock(job_id: str) -> threading.RLock:
    key = str(job_id or "").strip()
    with _UPLOAD_LOCKS_GUARD:
        lock = _UPLOAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _UPLOAD_LOCKS[key] = lock
        return lock


def _safe_display_name(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    return (text or f"SubCut_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}")[:120]


def _safe_filename(value: Any) -> str:
    raw = Path(str(value or "video.mp4").replace("\\", "/")).name.strip()
    stem = Path(raw).stem
    suffix = Path(raw).suffix.lower()
    safe_stem = "".join(ch if ch.isalnum() or ch in {"-", "_", ".", " "} else "_" for ch in stem)
    safe_stem = "_".join(safe_stem.split()).strip("._-")[:120] or "video"
    return f"{safe_stem}{suffix}"


def _sanitize_settings(raw: Any, *, display_name: str) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    clean = {key: source[key] for key in _ALLOWED_SETTINGS if key in source}
    clean["display_name"] = display_name
    clean["source_app"] = str(clean.get("source_app") or "sj88_subcut_studio")[:64]
    return clean


def _is_inside(target: Path, root: Path) -> bool:
    resolved_target = target.resolve()
    resolved_root = root.resolve()
    return resolved_target == resolved_root or resolved_root in resolved_target.parents


def _user_scope(user_id: int, *, create: bool = False) -> Path:
    scope = (USER_WORKSPACE_ROOT / str(max(0, int(user_id)))).resolve()
    if create:
        scope.mkdir(parents=True, exist_ok=True)
    return scope


def _resolve_job_root(job: dict[str, Any], *, user_id: int, create: bool = False) -> Path:
    raw = str(job.get("product_path") or "").strip()
    if not raw:
        raise HTTPException(status_code=409, detail="job_workspace_missing")
    root = Path(raw).expanduser().resolve()
    # Ownership is enforced by the database query before this check. Allow an
    # owned job to keep its original browser-guest directory after that guest
    # is merged into a member account; moving a running workspace is unsafe.
    workspace_root = USER_WORKSPACE_ROOT.resolve()
    if create:
        workspace_root.mkdir(parents=True, exist_ok=True)
    allowed = (workspace_root, LEGACY_UI_JOBS_DIR.resolve())
    if not any(_is_inside(root, base) for base in allowed):
        raise HTTPException(status_code=403, detail="job_workspace_outside_storage_root")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail="job_workspace_not_found")
    return root


def _owned_job(store: JobStore, job_id: str, user_id: int) -> dict[str, Any]:
    job = store.get_job(str(job_id), user_id=user_id)
    if not job or str(job.get("mode") or "").lower() not in _SUBCUT_MODES:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _session_path(root: Path) -> Path:
    return root / _UPLOAD_SESSION


def _read_json(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except FileNotFoundError:
        return default
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"corrupt_upload_state: {exc}") from exc


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _reset_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for item in list(root.iterdir()):
        _remove_path(item)


def _new_session(*, transport: str, total_files: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": 1,
        "transport": transport,
        "state": "uploading",
        "total_files": total_files,
        "files": {},
        "created_at": now,
        "updated_at": now,
        "finalized_at": "",
    }


def _load_or_initialize_session(
    root: Path,
    *,
    transport: str,
    total_files: int,
    allow_initialize: bool,
) -> dict[str, Any]:
    if total_files < 1 or total_files > _MAX_FILES:
        raise HTTPException(status_code=400, detail=f"total_files must be 1-{_MAX_FILES}")
    session = _read_json(_session_path(root), default=None)
    if session is None:
        if not allow_initialize:
            raise HTTPException(status_code=409, detail="upload_session_not_initialized")
        _reset_workspace(root)
        session = _new_session(transport=transport, total_files=total_files)
        _write_json_atomic(_session_path(root), session)
        return session
    if not isinstance(session, dict):
        raise HTTPException(status_code=409, detail="invalid_upload_session")
    if str(session.get("transport") or "") != transport:
        raise HTTPException(status_code=409, detail="upload_transport_conflict")
    if int(session.get("total_files") or 0) != total_files:
        raise HTTPException(status_code=409, detail="upload_total_files_conflict")
    if str(session.get("state") or "") not in {"uploading", "complete"}:
        raise HTTPException(status_code=409, detail="upload_session_not_writable")
    return session


def _allocate_destination(session: dict[str, Any], *, file_index: int, file_name: str) -> str:
    files = session.setdefault("files", {})
    existing = files.get(str(file_index))
    if isinstance(existing, dict) and existing.get("destination_name"):
        return str(existing["destination_name"])
    used = {
        str(item.get("destination_name") or "").lower()
        for item in files.values()
        if isinstance(item, dict)
    }
    safe_name = _safe_filename(file_name)
    candidate = safe_name
    sequence = 2
    while candidate.lower() in used:
        path = Path(safe_name)
        candidate = f"{path.stem}_{sequence}{path.suffix}"
        sequence += 1
    return candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save_upload(
    upload: UploadFile,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.upload.{os.getpid()}.{threading.get_ident()}")
    digest = hashlib.sha256()
    total = 0
    try:
        with temp.open("wb") as output:
            while True:
                block = upload.file.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > _MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="file_too_large")
                digest.update(block)
                output.write(block)
        if total <= 0:
            raise HTTPException(status_code=400, detail="empty_file")
        if expected_size is not None and expected_size >= 0 and total != expected_size:
            raise HTTPException(status_code=409, detail="uploaded_size_mismatch")
        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != str(expected_sha256).strip().lower():
            raise HTTPException(status_code=409, detail="chunk_sha256_mismatch")
        temp.replace(destination)
        return total, actual_sha256
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _validate_video_name(name: str) -> str:
    safe = _safe_filename(name)
    if Path(safe).suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=415, detail=f"unsupported_video_type: {Path(safe).suffix or 'none'}")
    return safe


def _manifest_rows(session: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, item in sorted(
        (session.get("files") or {}).items(),
        key=lambda pair: int(pair[0]),
    ):
        if not isinstance(item, dict) or not item.get("assembled"):
            continue
        rows.append(
            {
                "name": str(item.get("file_name") or item.get("destination_name") or ""),
                "relpath": str(item.get("destination_name") or ""),
                "size": int(item.get("assembled_size") or 0),
                "sha256": str(item.get("assembled_sha256") or ""),
                "file_index": int(item.get("file_index") or int(key)),
            }
        )
    return rows


def _write_input_manifest(root: Path, *, mode: str, session: dict[str, Any]) -> None:
    rows = _manifest_rows(session)
    _write_json_atomic(
        root / _INPUT_MANIFEST,
        {"version": 2, "mode": mode, "files": rows},
    )


def _uploaded_inputs(root: Path) -> list[Path]:
    manifest = _read_json(root / _INPUT_MANIFEST, default={})
    rows = manifest.get("files") if isinstance(manifest, dict) else []
    output: list[Path] = []
    seen: set[str] = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        relpath = str(item.get("relpath") or "").strip()
        if not relpath:
            continue
        candidate = (root / relpath).resolve()
        if not _is_inside(candidate, root) or not candidate.exists() or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in VIDEO_EXTS:
            continue
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            output.append(candidate)
    return output


def _iter_output_files(root: Path) -> Iterator[Path]:
    output_root = root / "output"
    if not output_root.exists() or not output_root.is_dir():
        return
    for item in sorted(output_root.rglob("*")):
        if not item.is_file():
            continue
        try:
            rel = item.relative_to(output_root)
        except ValueError:
            continue
        if any(part.startswith(".") or part == "trim_work" for part in rel.parts):
            continue
        yield item


def _duration_seconds(job: dict[str, Any]) -> float:
    def parse(value: Any) -> datetime | None:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    start = parse(job.get("started_at")) or parse(job.get("created_at"))
    end = parse(job.get("finished_at")) or parse(job.get("updated_at"))
    return max(0.0, (end - start).total_seconds()) if start and end else 0.0


def _duration_text(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _history_payload(job: dict[str, Any]) -> dict[str, Any]:
    settings = dict(job.get("settings") or {})
    return {
        "id": str(job.get("id") or ""),
        "name": str(settings.get("display_name") or Path(str(job.get("product_path") or "")).name),
        "mode": str(job.get("mode") or ""),
        "status": str(job.get("status") or ""),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
        "started_at": str(job.get("started_at") or ""),
        "finished_at": str(job.get("finished_at") or ""),
        "duration_str": _duration_text(_duration_seconds(job)),
        "total_input_bytes": int(job.get("total_input_bytes") or 0),
        "total_output_bytes": int(job.get("total_output_bytes") or 0),
        "total_outputs": int(job.get("total_outputs") or 0),
        "settings": settings,
        "outcome": dict(job.get("outcome") or {}),
        "error": str(job.get("error") or ""),
    }


@router.post("/api/jobs")
def create_job(payload: dict, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    body = payload if isinstance(payload, dict) else {}
    mode = str(body.get("mode") or "autosu_only").strip().lower()
    if mode not in _SUBCUT_MODES:
        raise HTTPException(status_code=400, detail="unsupported_subcut_mode")
    display_name = _safe_display_name(body.get("name"))
    settings = _sanitize_settings(body.get("settings"), display_name=display_name)
    workspace = _user_scope(user_id, create=True) / "subcut_jobs"
    workspace.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    unique = hashlib.sha256(f"{time.time_ns()}:{user_id}:{display_name}".encode()).hexdigest()[:10]
    root = (workspace / f"{stamp}-{unique}").resolve()
    root.mkdir(parents=True, exist_ok=False)
    job = store.create_job(
        product_path=str(root),
        mode=mode,
        settings=settings,
        status="created",
        user_id=user_id,
    )
    store.add_event(job["id"], f"SubCut job created: mode={mode}, name={display_name}")
    return {"ok": True, "job": job_to_legacy(job, store)}


@router.get("/api/jobs")
def list_jobs(
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(require_request_user),
) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    rows = [
        job_to_legacy(job, store)
        for job in store.list_jobs(limit=min(500, max(limit * 3, limit)), user_id=user_id)
        if str(job.get("mode") or "").lower() in _SUBCUT_MODES
    ][:limit]
    return {"ok": True, "total": len(rows), "jobs": rows}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    return job_to_legacy(job, store, include_details=True)


@router.post("/api/jobs/{job_id}/upload")
def upload_direct(
    job_id: str,
    files: list[UploadFile] = File(...),
    append: int = Query(default=0, ge=0, le=1),
    file_index: int = Query(default=0, ge=0),
    total_files: int = Query(default=1, ge=1),
    file_size: int = Query(default=-1, ge=-1),
    current_user: dict = Depends(require_request_user),
) -> dict[str, Any]:
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="upload_one_file_per_request")
    if file_index >= total_files:
        raise HTTPException(status_code=400, detail="file_index_out_of_range")
    if file_size > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    if str(job.get("status")) not in {"created", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="job_not_uploadable")
    root = _resolve_job_root(job, user_id=user_id, create=True)
    upload = files[0]
    original_name = str(upload.filename or "video.mp4")
    _validate_video_name(original_name)

    with _job_lock(job_id):
        session = _load_or_initialize_session(
            root,
            transport="direct",
            total_files=total_files,
            allow_initialize=(file_index == 0 and append == 0),
        )
        destination_name = _allocate_destination(session, file_index=file_index, file_name=original_name)
        destination = root / destination_name
        actual_size, digest = _save_upload(
            upload,
            destination,
            expected_size=file_size if file_size >= 0 else None,
        )
        session.setdefault("files", {})[str(file_index)] = {
            "file_index": file_index,
            "file_name": _safe_filename(original_name),
            "destination_name": destination_name,
            "file_size": actual_size,
            "assembled": True,
            "assembled_size": actual_size,
            "assembled_sha256": digest,
        }
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        if len(_manifest_rows(session)) == total_files:
            session["state"] = "complete"
            session["finalized_at"] = session["updated_at"]
        _write_json_atomic(_session_path(root), session)
        _write_input_manifest(root, mode=str(job.get("mode") or ""), session=session)

    store.add_event(job_id, f"Uploaded {destination_name} ({actual_size} bytes)")
    return {
        "ok": True,
        "saved": [{"name": destination_name, "size": actual_size, "sha256": digest}],
        "uploaded_files": len(_manifest_rows(session)),
        "total_files": total_files,
    }


@router.post("/api/jobs/{job_id}/upload/chunk")
def upload_chunk(
    job_id: str,
    files: list[UploadFile] = File(...),
    chunk_index: int = Query(..., ge=0),
    total_chunks: int = Query(..., ge=1),
    total_files: int = Query(..., ge=1),
    file_name: str = Query(..., min_length=1),
    file_size: int = Query(..., ge=1),
    file_index: int = Query(..., ge=0),
    chunk_sha256: str = Query(..., min_length=64, max_length=64),
    chunk_manifest_sha256: str = Query(..., min_length=64, max_length=64),
    append: int = Query(default=0, ge=0, le=1),
    current_user: dict = Depends(require_request_user),
) -> dict[str, Any]:
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="upload_one_chunk_per_request")
    if file_index >= total_files or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="chunk_index_out_of_range")
    if file_size > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")
    expected_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
    if total_chunks != expected_chunks or total_chunks > _MAX_CHUNKS:
        raise HTTPException(status_code=400, detail="invalid_total_chunks")
    chunk_sha256 = chunk_sha256.strip().lower()
    chunk_manifest_sha256 = chunk_manifest_sha256.strip().lower()
    if not _HEX_64.fullmatch(chunk_sha256) or not _HEX_64.fullmatch(chunk_manifest_sha256):
        raise HTTPException(status_code=400, detail="invalid_sha256")
    _validate_video_name(file_name)

    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    if str(job.get("status")) not in {"created", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="job_not_uploadable")
    root = _resolve_job_root(job, user_id=user_id, create=True)

    with _job_lock(job_id):
        session = _load_or_initialize_session(
            root,
            transport="chunked",
            total_files=total_files,
            allow_initialize=(file_index == 0 and chunk_index == 0 and append == 0),
        )
        destination_name = _allocate_destination(session, file_index=file_index, file_name=file_name)
        entry = session.setdefault("files", {}).get(str(file_index))
        metadata = {
            "file_index": file_index,
            "file_name": _safe_filename(file_name),
            "destination_name": destination_name,
            "file_size": file_size,
            "total_chunks": total_chunks,
            "chunk_manifest_sha256": chunk_manifest_sha256,
        }
        if isinstance(entry, dict):
            for key, expected in metadata.items():
                if str(entry.get(key)) != str(expected):
                    raise HTTPException(status_code=409, detail=f"chunk_metadata_conflict:{key}")
        else:
            entry = {**metadata, "chunks": {}, "assembled": False}
            session["files"][str(file_index)] = entry

        expected_chunk_size = min(CHUNK_SIZE, file_size - (chunk_index * CHUNK_SIZE))
        if expected_chunk_size <= 0:
            raise HTTPException(status_code=400, detail="invalid_chunk_size")
        chunk_path = root / _CHUNK_DIR / str(file_index) / f"{chunk_index:08d}.part"
        actual_size, actual_hash = _save_upload(
            files[0],
            chunk_path,
            expected_size=expected_chunk_size,
            expected_sha256=chunk_sha256,
        )
        chunks = entry.setdefault("chunks", {})
        recorded = chunks.get(str(chunk_index))
        if isinstance(recorded, dict) and (
            int(recorded.get("size") or -1) != actual_size
            or str(recorded.get("sha256") or "") != actual_hash
        ):
            raise HTTPException(status_code=409, detail="chunk_retry_conflict")
        chunks[str(chunk_index)] = {"size": actual_size, "sha256": actual_hash}
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(_session_path(root), session)

    store.add_event(job_id, f"Uploaded chunk {chunk_index + 1}/{total_chunks} for {destination_name}")
    return {
        "ok": True,
        "file_index": file_index,
        "chunk_index": chunk_index,
        "received_chunks": len(entry.get("chunks") or {}),
        "total_chunks": total_chunks,
    }


@router.post("/api/jobs/{job_id}/upload/chunked/complete")
def complete_chunked_upload(
    job_id: str,
    current_user: dict = Depends(require_request_user),
) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    if str(job.get("status")) not in {"created", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="job_not_uploadable")
    root = _resolve_job_root(job, user_id=user_id, create=True)

    with _job_lock(job_id):
        session = _read_json(_session_path(root), default=None)
        if not isinstance(session, dict) or session.get("transport") != "chunked":
            raise HTTPException(status_code=409, detail="chunked_upload_session_missing")
        total_files = int(session.get("total_files") or 0)
        file_entries = session.get("files") or {}
        if total_files < 1 or len(file_entries) != total_files:
            raise HTTPException(status_code=409, detail="not_all_files_registered")

        for file_index in range(total_files):
            entry = file_entries.get(str(file_index))
            if not isinstance(entry, dict):
                raise HTTPException(status_code=409, detail=f"file_entry_missing:{file_index}")
            total_chunks = int(entry.get("total_chunks") or 0)
            chunks = entry.get("chunks") or {}
            if total_chunks < 1 or len(chunks) != total_chunks:
                raise HTTPException(status_code=409, detail=f"chunks_incomplete:{file_index}")
            ordered_hashes: list[str] = []
            for chunk_index in range(total_chunks):
                record = chunks.get(str(chunk_index))
                chunk_path = root / _CHUNK_DIR / str(file_index) / f"{chunk_index:08d}.part"
                if not isinstance(record, dict) or not chunk_path.exists():
                    raise HTTPException(status_code=409, detail=f"chunk_missing:{file_index}:{chunk_index}")
                expected_hash = str(record.get("sha256") or "")
                if _hash_file(chunk_path) != expected_hash:
                    raise HTTPException(status_code=409, detail=f"chunk_corrupt:{file_index}:{chunk_index}")
                ordered_hashes.append(expected_hash)
            actual_manifest_hash = hashlib.sha256("".join(ordered_hashes).encode("utf-8")).hexdigest()
            if actual_manifest_hash != str(entry.get("chunk_manifest_sha256") or ""):
                raise HTTPException(status_code=409, detail=f"chunk_manifest_mismatch:{file_index}")

            destination = root / str(entry.get("destination_name") or "")
            temp = destination.with_name(f".{destination.name}.assemble.{os.getpid()}.{threading.get_ident()}")
            digest = hashlib.sha256()
            total_size = 0
            try:
                with temp.open("wb") as output:
                    for chunk_index in range(total_chunks):
                        chunk_path = root / _CHUNK_DIR / str(file_index) / f"{chunk_index:08d}.part"
                        with chunk_path.open("rb") as source:
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                output.write(block)
                                digest.update(block)
                                total_size += len(block)
                if total_size != int(entry.get("file_size") or -1):
                    raise HTTPException(status_code=409, detail=f"assembled_size_mismatch:{file_index}")
                temp.replace(destination)
            finally:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
            entry["assembled"] = True
            entry["assembled_size"] = total_size
            entry["assembled_sha256"] = digest.hexdigest()

        session["state"] = "complete"
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        session["finalized_at"] = session["updated_at"]
        _write_json_atomic(_session_path(root), session)
        _write_input_manifest(root, mode=str(job.get("mode") or ""), session=session)
        _remove_path(root / _CHUNK_DIR)

    store.add_event(job_id, f"Chunked upload finalized: files={total_files}")
    return {"ok": True, "files": _manifest_rows(session), "total_files": total_files}


@router.post("/api/jobs/{job_id}/process")
def queue_job(job_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    root = _resolve_job_root(job, user_id=user_id)
    inputs = _uploaded_inputs(root)
    if not inputs:
        raise HTTPException(status_code=409, detail="upload_required_before_processing")
    if not store.queue_job(job_id, user_id=user_id):
        refreshed = store.get_job(job_id, user_id=user_id)
        status = str((refreshed or {}).get("status") or "")
        if status in {"queued", "running"}:
            return {"ok": True, "already_queued": True, "status": status}
        raise HTTPException(status_code=409, detail=f"cannot_queue_job_from_status:{status or 'unknown'}")
    store.add_event(job_id, f"Queued for background processing: inputs={len(inputs)}")
    return {"ok": True, "status": "queued", "input_count": len(inputs)}


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    status = str(job.get("status") or "")
    changed = False
    if status == "created":
        changed = store.finish_job(
            job_id,
            status="cancelled",
            outcome={},
            error="Cancelled before queue",
            total_outputs=0,
            total_output_bytes=0,
        )
    elif status == "queued":
        changed = store.cancel_queued_job(job_id, user_id=user_id)
    elif status == "running":
        changed = store.request_cancel(job_id, user_id=user_id)
    elif status in _TERMINAL_STATUSES:
        return {"ok": True, "status": status, "already_terminal": True}
    if not changed:
        raise HTTPException(status_code=409, detail="cancel_request_not_applied")
    store.add_event(job_id, f"Cancellation requested by user: previous_status={status}", level="warn")
    return {"ok": True, "status": "cancelling" if status == "running" else "cancelled"}


@router.get("/api/history")
def list_history(
    limit: int = Query(default=200, ge=1, le=500),
    current_user: dict = Depends(require_request_user),
) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    rows = [
        _history_payload(job)
        for job in store.list_completed_jobs(limit=min(1000, limit * 4), user_id=user_id)
        if str(job.get("mode") or "").lower() in _SUBCUT_MODES
        and str(job.get("status") or "").lower() == "done"
    ][:limit]
    return {"ok": True, "total": len(rows), "items": rows}


@router.get("/api/history/{job_id}")
def history_detail(job_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    root = _resolve_job_root(job, user_id=user_id)
    files = [
        {
            "name": path.relative_to(root).as_posix(),
            "path": path.relative_to(root).as_posix(),
            "size": int(path.stat().st_size),
        }
        for path in _iter_output_files(root)
    ]
    return {"ok": True, "job": _history_payload(job), "output_files": files}


@router.get("/api/jobs/{job_id}/download/output/direct")
def download_all_outputs(job_id: str, current_user: dict = Depends(require_request_user)) -> FileResponse:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    root = _resolve_job_root(job, user_id=user_id)
    files = list(_iter_output_files(root))
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")
    display_name = _safe_display_name((job.get("settings") or {}).get("display_name"))
    zip_path = cached_zip_selected_files(root, files=files, filename_prefix=f"{display_name}_{job_id[:8]}")
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"{_safe_filename(display_name).rsplit('.', 1)[0]}_outputs.zip",
    )


@router.get("/api/history/{job_id}/files/{file_path:path}")
def download_one_output(
    job_id: str,
    file_path: str,
    current_user: dict = Depends(require_request_user),
) -> FileResponse:
    store = _get_store()
    user_id = user_id_from_user(current_user)
    job = _owned_job(store, job_id, user_id)
    root = _resolve_job_root(job, user_id=user_id)
    output_root = (root / "output").resolve()
    relative = str(file_path or "").replace("\\", "/").lstrip("/")
    if relative == "output":
        relative = ""
    elif relative.startswith("output/"):
        relative = relative[len("output/") :]
    target = (output_root / relative).resolve()
    if not relative or not _is_inside(target, output_root) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Output file not found")
    if any(part.startswith(".") or part == "trim_work" for part in target.relative_to(output_root).parts):
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(path=str(target), filename=target.name, media_type="application/octet-stream")
