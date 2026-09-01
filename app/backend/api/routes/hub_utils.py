"""Shared helpers for the Bold workspace APIs."""

from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ...config import DB_PATH, STORAGE_QUOTA_BYTES, USER_WORKSPACE_ROOT
from ...services.constants import VIDEO_EXTS
from ...services.job_store import JobStore
from ...services.legacy import job_to_legacy
from ...services.workspace_service import WorkspaceService

STORE = JobStore(DB_PATH)
WORKSPACE = WorkspaceService(DB_PATH)
TERMINAL = {"done", "failed", "error", "cancelled"}
UPLOAD_SESSION = ".subcut_upload_session.json"


def owned_job(job_id: str, user_id: int) -> dict[str, Any]:
    job = STORE.get_job(str(job_id), user_id=int(user_id))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def safe_job_root(job: dict[str, Any], *, must_exist: bool = True) -> Path:
    raw = str(job.get("product_path") or "").strip()
    if not raw:
        raise HTTPException(status_code=409, detail="job_workspace_missing")
    root = Path(raw).expanduser().resolve()
    base = USER_WORKSPACE_ROOT.resolve()
    if root != base and base not in root.parents:
        raise HTTPException(status_code=403, detail="job_workspace_outside_storage_root")
    if must_exist and (not root.exists() or not root.is_dir()):
        raise HTTPException(status_code=404, detail="job_workspace_not_found")
    return root


def read_upload_state(root: Path) -> dict[str, Any]:
    path = root / UPLOAD_SESSION
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "none", "percent": 0, "uploaded_bytes": 0, "total_bytes": 0, "files": []}
    files = session.get("files") if isinstance(session, dict) else {}
    output: list[dict[str, Any]] = []
    uploaded_bytes = 0
    total_bytes = 0
    for raw in (files or {}).values():
        if not isinstance(raw, dict):
            continue
        file_size = max(0, int(raw.get("file_size") or raw.get("assembled_size") or 0))
        total_bytes += file_size
        if raw.get("assembled"):
            received = file_size
        else:
            received = sum(max(0, int(item.get("size") or 0)) for item in (raw.get("chunks") or {}).values() if isinstance(item, dict))
        uploaded_bytes += min(file_size, received)
        output.append({
            "file_index": int(raw.get("file_index") or 0),
            "name": str(raw.get("destination_name") or raw.get("file_name") or "video"),
            "size": file_size,
            "uploaded_bytes": min(file_size, received),
            "total_chunks": int(raw.get("total_chunks") or 1),
            "received_chunks": len(raw.get("chunks") or {}) if not raw.get("assembled") else int(raw.get("total_chunks") or 1),
            "received_indices": sorted(int(key) for key in (raw.get("chunks") or {}).keys() if str(key).isdigit()),
            "assembled": bool(raw.get("assembled")),
        })
    percent = round((uploaded_bytes / total_bytes) * 100, 1) if total_bytes else (100 if session.get("state") == "complete" else 0)
    return {
        "state": str(session.get("state") or "uploading"),
        "transport": str(session.get("transport") or ""),
        "percent": percent,
        "uploaded_bytes": uploaded_bytes,
        "total_bytes": total_bytes,
        "updated_at": str(session.get("updated_at") or ""),
        "files": sorted(output, key=lambda item: item["file_index"]),
    }


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _average_runtime(jobs: list[dict[str, Any]]) -> float:
    samples: list[float] = []
    for job in jobs:
        if str(job.get("status")) != "done":
            continue
        start, end = _parse_time(job.get("started_at")), _parse_time(job.get("finished_at"))
        if start and end and end > start:
            samples.append((end - start).total_seconds())
    return sum(samples[-20:]) / len(samples[-20:]) if samples else 180.0


def stage_steps(job: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    status = str(job.get("status") or "created").lower()
    progress = int(payload.get("progress") or 0)
    mode = str(job.get("mode") or "")
    workflow = str((job.get("settings") or {}).get("workflow") or "subtitle")
    labels = ["อัปโหลด", "ตรวจไฟล์", "รอคิว"]
    if mode == "silence_trim_only" or workflow == "combined":
        labels.append("ตัดเสียงเงียบ")
    if mode == "autosu_only":
        labels.append("ถอดเสียงและทำซับ")
    labels.extend(["สร้างไฟล์", "พร้อมดาวน์โหลด"])
    active_index = 0
    if status == "created":
        active_index = 0
    elif status == "queued":
        active_index = 2
    elif status == "running":
        active_index = min(len(labels) - 2, 3 + int(progress >= 70))
    elif status == "done":
        active_index = len(labels) - 1
    elif status in {"failed", "error", "cancelled"}:
        active_index = max(1, min(len(labels) - 2, 2 + int(progress > 15) + int(progress > 70)))
    steps = []
    for index, label in enumerate(labels):
        state = "done" if index < active_index or status == "done" else "active" if index == active_index else "pending"
        if status in {"failed", "error"} and index == active_index:
            state = "error"
        if status == "cancelled" and index == active_index:
            state = "cancelled"
        steps.append({"key": f"step-{index}", "label": label, "state": state})
    return steps


def job_snapshot(user_id: int, *, limit: int = 500) -> dict[str, Any]:
    raw_jobs = STORE.list_jobs(limit=max(1, min(1000, int(limit))), user_id=int(user_id))
    metadata = WORKSPACE.metadata_map(user_id, [str(job.get("id")) for job in raw_jobs])
    queued = [job for job in raw_jobs if str(job.get("status")) == "queued"]
    queue_index = {str(job.get("id")): index + 1 for index, job in enumerate(reversed(queued))}
    average_runtime = _average_runtime(raw_jobs)
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for job in raw_jobs:
        payload = job_to_legacy(job, STORE)
        meta = metadata.get(str(job.get("id")), {})
        if meta.get("display_name"):
            payload["name"] = meta["display_name"]
        payload["library"] = {
            "pinned": bool(meta.get("pinned")), "favorite": bool(meta.get("favorite")),
            "folder_id": str(meta.get("folder_id") or ""), "tags": list(meta.get("tags") or []),
            "retention_until": str(meta.get("retention_until") or ""),
        }
        status = str(job.get("status") or "")
        position = queue_index.get(str(job.get("id")), 0)
        payload["queue_position"] = position
        if status == "queued":
            payload["eta_seconds"] = int(max(30, position * average_runtime))
        elif status == "running":
            started = _parse_time(job.get("started_at"))
            elapsed = (now - started).total_seconds() if started else 0
            progress = max(1, int(payload.get("progress") or 1))
            payload["eta_seconds"] = int(max(5, (elapsed / progress) * (100 - progress)))
        else:
            payload["eta_seconds"] = 0
        scan = job.get("scan_summary") if isinstance(job.get("scan_summary"), dict) else {}
        payload["stage"] = str(scan.get("stage") or ("พร้อมดาวน์โหลด" if status == "done" else ""))
        payload["steps"] = stage_steps(job, payload)
        try:
            payload["upload"] = read_upload_state(safe_job_root(job))
        except HTTPException:
            payload["upload"] = {"state": "missing", "percent": 0, "files": []}
        payload["started_at"] = str(job.get("started_at") or "")
        payload["finished_at"] = str(job.get("finished_at") or "")
        payload["input_bytes"] = int(job.get("total_input_bytes") or payload["upload"].get("total_bytes") or 0)
        payload["output_bytes"] = int(job.get("total_output_bytes") or 0)
        payload["failure_code"] = str(job.get("failure_code") or "")
        items.append(payload)
    counts = {key: sum(1 for item in items if item["status"] in values) for key, values in {
        "uploading": {"created"}, "queued": {"queued"}, "processing": {"running"},
        "done": {"done"}, "failed": {"error", "failed", "cancelled"},
    }.items()}
    latest = max((item.get("updated_at") or "" for item in items), default="")
    return {"jobs": items, "counts": counts, "average_runtime_seconds": int(average_runtime), "version": f"{latest}:{len(items)}"}


def iter_sources(root: Path) -> list[Path]:
    files = [path for path in root.iterdir() if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in VIDEO_EXTS]
    return sorted(files)


def iter_outputs(root: Path) -> list[Path]:
    output = root / "output"
    if not output.exists():
        return []
    return sorted(path for path in output.rglob("*") if path.is_file() and not any(part.startswith(".") or part == "trim_work" for part in path.relative_to(output).parts))


def file_payload(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {"name": path.name, "path": path.relative_to(root).as_posix(), "size": int(stat.st_size),
            "mime": mime, "previewable": mime.startswith(("video/", "audio/", "text/")) or path.suffix.lower() in {".srt", ".vtt", ".ass"}}


def storage_stats(user_id: int) -> dict[str, Any]:
    root = USER_WORKSPACE_ROOT / str(int(user_id))
    buckets = {"source": 0, "output": 0, "upload": 0, "cache": 0, "other": 0}
    if root.exists():
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if not name.startswith(".tmp")]
            folder = Path(current)
            for name in files:
                path = folder / name
                try:
                    size = int(path.stat().st_size)
                except OSError:
                    continue
                rel = path.relative_to(root).parts
                if "output" in rel:
                    buckets["output"] += size
                elif ".subcut_chunks" in rel or name == UPLOAD_SESSION:
                    buckets["upload"] += size
                elif path.suffix.lower() in VIDEO_EXTS:
                    buckets["source"] += size
                elif "cache" in rel:
                    buckets["cache"] += size
                else:
                    buckets["other"] += size
    total = sum(buckets.values())
    quota = STORAGE_QUOTA_BYTES
    return {"total_bytes": total, "quota_bytes": quota, "remaining_bytes": max(0, quota - total), "percent": round(total / quota * 100, 2), "buckets": buckets}
