"""Subtitle Editor Lite APIs for SRT, WebVTT, and ASS."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ._auth_guard import require_request_user, user_id_from_user
from .hub_utils import STORE, owned_job, safe_job_root
from ...services.subtitle_formats import detect_format, normalize_cues, parse_subtitles, render_subtitles

router = APIRouter(tags=["Subtitle Editor"])
SUBTITLE_EXTS = {".srt", ".vtt", ".ass"}
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def _output_root(job_id: str, user_id: int) -> Path:
    root = safe_job_root(owned_job(job_id, user_id))
    output = (root / "output").resolve()
    output.mkdir(parents=True, exist_ok=True)
    return output


def _safe_subtitle(output: Path, requested: str = "") -> Path | None:
    if requested:
        clean = str(requested).replace("\\", "/").lstrip("/")
        if clean.startswith("output/"):
            clean = clean[7:]
        target = (output / clean).resolve()
        if output in target.parents and target.suffix.lower() in SUBTITLE_EXTS and target.is_file():
            return target
    candidates = [path for path in output.rglob("*") if path.is_file() and path.suffix.lower() in SUBTITLE_EXTS]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def _clean_basename(value: Any) -> str:
    name = re.sub(r"[^0-9A-Za-zก-๙._-]+", "_", str(value or "edited_subtitles").strip())
    name = Path(name).stem.strip("._")[:80]
    return name or "edited_subtitles"


@router.get("/api/jobs/{job_id}/subtitles")
def get_subtitles(job_id: str, file_path: str = "", current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    output = _output_root(job_id, user_id)
    target = _safe_subtitle(output, file_path)
    if not target:
        return {"ok": True, "format": "srt", "file": "", "cues": [], "empty": True}
    try:
        content = target.read_text(encoding="utf-8-sig", errors="replace")
        fmt, cues = parse_subtitles(target.name, content)
    except OSError as exc:
        raise HTTPException(status_code=422, detail="subtitle_read_failed") from exc
    return {"ok": True, "format": fmt, "file": target.relative_to(output).as_posix(), "cues": cues, "empty": False}


@router.post("/api/jobs/{job_id}/subtitles/import")
def import_subtitles(job_id: str, payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    _output_root(job_id, user_id)
    filename = str(payload.get("filename") or "subtitles.srt")
    content = str(payload.get("content") or "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="subtitle_content_required")
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="subtitle_file_too_large")
    fmt, cues = parse_subtitles(filename, content)
    if not cues:
        raise HTTPException(status_code=422, detail="subtitle_cues_not_found")
    return {"ok": True, "format": fmt, "filename": filename[:255], "cues": cues}


@router.post("/api/jobs/{job_id}/subtitles/export")
def export_subtitles(job_id: str, payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    _output_root(job_id, user_id)
    fmt = detect_format(f"file.{str(payload.get('format') or 'srt').lower()}")
    cues = normalize_cues(payload.get("cues"))
    if not cues:
        raise HTTPException(status_code=422, detail="subtitle_cues_required")
    filename = f"{_clean_basename(payload.get('base_name'))}.{fmt}"
    return {"ok": True, "format": fmt, "filename": filename, "mime": {"srt": "application/x-subrip", "vtt": "text/vtt", "ass": "text/x-ssa"}[fmt], "content": render_subtitles(cues, fmt)}


@router.put("/api/jobs/{job_id}/subtitles")
def save_subtitles(job_id: str, payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    output = _output_root(job_id, user_id)
    cues = normalize_cues(payload.get("cues"))
    if not cues:
        raise HTTPException(status_code=422, detail="subtitle_cues_required")
    requested = payload.get("formats") or [payload.get("format") or "srt"]
    formats = [fmt for fmt in dict.fromkeys(str(item).lower() for item in requested) if fmt in {"srt", "vtt", "ass"}]
    if not formats:
        formats = ["srt"]
    directory = output / "subtitles"
    directory.mkdir(parents=True, exist_ok=True)
    base = _clean_basename(payload.get("base_name"))
    files = []
    for fmt in formats:
        target = directory / f"{base}.{fmt}"
        target.write_text(render_subtitles(cues, fmt), encoding="utf-8")
        files.append({"name": target.name, "path": target.relative_to(output.parent).as_posix(), "size": target.stat().st_size, "format": fmt})
    STORE.add_event(job_id, f"Subtitle Editor saved {len(cues)} cues: {','.join(formats)}")
    return {"ok": True, "cues": cues, "files": files}
