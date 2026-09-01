"""Generate a short 10–20 second silence-trim preview without queueing a full job."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ._auth_guard import require_request_user, user_id_from_user
from .hub_utils import iter_sources, owned_job, safe_job_root
from ...services.subtitle_trim_engine import trim_silence_proxy
from ...services.subtitle_trim_settings import SubtitleTrimRuntime, normalize_subtitle_trim_settings

router = APIRouter(tags=["Media Preview"])


def _preview_root(job_id: str, user_id: int) -> tuple[Path, Path]:
    root = safe_job_root(owned_job(job_id, user_id))
    preview = (root / ".preview").resolve()
    preview.mkdir(parents=True, exist_ok=True)
    return root, preview


def _ffmpeg_segment(source: Path, target: Path, start: float, duration: float) -> None:
    command = [
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="ffmpeg_not_found") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="trim_preview_timeout") from exc
    if result.returncode != 0 or not target.is_file():
        raise HTTPException(status_code=422, detail=f"trim_preview_extract_failed:{result.stderr[-160:]}")


@router.post("/api/jobs/{job_id}/trim-preview")
def create_trim_preview(job_id: str, payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    root, preview = _preview_root(job_id, user_id)
    sources = iter_sources(root)
    if not sources:
        raise HTTPException(status_code=404, detail="preview_source_not_found")
    start = max(0.0, float(payload.get("start_sec") or 0.0))
    duration = max(10.0, min(20.0, float(payload.get("duration_sec") or 15.0)))
    settings = normalize_subtitle_trim_settings({**(payload.get("settings") or {}), "trim_silence": True})
    source = sources[0]
    fingerprint = json.dumps({"source": source.name, "mtime": source.stat().st_mtime_ns, "start": start, "duration": duration, "settings": settings}, sort_keys=True)
    key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:14]
    sample = preview / f"source_{key}.mp4"
    if not sample.exists():
        _ffmpeg_segment(source, sample, start, duration)
    runtime = SubtitleTrimRuntime(**settings)
    logs: list[str] = []
    output, manifest = trim_silence_proxy(sample, preview / key, runtime, logs.append, ffmpeg_exe="ffmpeg")
    selected = output if output.is_file() else sample
    final = preview / f"trim_{key}.mp4"
    if selected != final and not final.exists():
        shutil.copy2(selected, final)
    for old in sorted(preview.glob("trim_*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)[6:]:
        old.unlink(missing_ok=True)
    return {
        "ok": True, "url": f"/api/jobs/{job_id}/trim-preview/media/{final.name}", "name": final.name,
        "start_sec": start, "requested_duration_sec": duration,
        "before_duration_sec": round(float(manifest.get("before_duration") or duration), 3),
        "after_duration_sec": round(float(manifest.get("after_duration") or manifest.get("kept_duration") or duration), 3),
        "removed_duration_sec": round(float(manifest.get("removed_duration") or 0.0), 3),
        "trim_applied": not bool(manifest.get("skipped", True)), "reason": str(manifest.get("reason") or "applied"),
    }


@router.get("/api/jobs/{job_id}/trim-preview/media/{filename}")
def trim_preview_media(job_id: str, filename: str, current_user: dict = Depends(require_request_user)) -> FileResponse:
    user_id = user_id_from_user(current_user)
    _root, preview = _preview_root(job_id, user_id)
    target = (preview / Path(filename).name).resolve()
    if preview not in target.parents or not target.is_file() or target.suffix.lower() != ".mp4":
        raise HTTPException(status_code=404, detail="trim_preview_not_found")
    return FileResponse(str(target), media_type="video/mp4", filename=target.name)
