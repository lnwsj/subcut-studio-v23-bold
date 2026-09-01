"""Bold workspace job hub, library, upload status, and media preview APIs."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from ._auth_guard import require_request_user, user_id_from_user
from ...config import OUTPUT_RETENTION_DAYS, OUTPUT_RETENTION_EXTENSION_MAX_DAYS

from .hub_utils import (
    STORE, TERMINAL, WORKSPACE, file_payload, iter_outputs, iter_sources,
    job_snapshot, owned_job, read_upload_state, safe_job_root, storage_stats,
)
from ...config import USER_WORKSPACE_ROOT

router = APIRouter(tags=["Workspace Hub"])


def _sync_terminal_notifications(user_id: int, jobs: list[dict[str, Any]]) -> None:
    for job in jobs:
        status = str(job.get("status") or "")
        if status not in {"done", "error", "failed"}:
            continue
        event_key = f"job:{job['id']}:{status}:{job.get('finished_at') or job.get('updated_at')}"
        success = status == "done"
        WORKSPACE.create_notification(
            user_id,
            event_key=event_key,
            kind="job_done" if success else "job_failed",
            severity="success" if success else "error",
            title="งานเสร็จพร้อมดาวน์โหลด" if success else "งานประมวลผลไม่สำเร็จ",
            body=f"{job.get('name') or 'งาน SubCut'} — " + ("เปิดดูตัวอย่างหรือดาวน์โหลดได้แล้ว" if success else str(job.get("error") or "กดลองใหม่ได้โดยไม่ต้องอัปโหลดซ้ำ")),
            job_id=str(job["id"]),
            action_url=f"/#job={job['id']}",
        )


def _snapshot_for(user_id: int) -> dict[str, Any]:
    payload = job_snapshot(user_id)
    _sync_terminal_notifications(user_id, payload["jobs"])
    payload["unread_notifications"] = WORKSPACE.unread_count(user_id)
    payload["folders"] = WORKSPACE.list_folders(user_id)
    payload["storage"] = storage_stats(user_id)
    return payload


@router.get("/api/hub")
def workspace_hub(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    return {"ok": True, **_snapshot_for(user_id_from_user(current_user))}


@router.get("/api/hub/stream")
async def workspace_stream(current_user: dict = Depends(require_request_user)) -> StreamingResponse:
    user_id = user_id_from_user(current_user)

    async def events() -> AsyncIterator[str]:
        last_version = ""
        yield "retry: 2500\n\n"
        while True:
            try:
                snapshot = _snapshot_for(user_id)
                version = f"{snapshot.get('version')}:{snapshot.get('unread_notifications')}"
                if version != last_version:
                    last_version = version
                    yield f"event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}\n\n"
                else:
                    yield f"event: heartbeat\ndata: {json.dumps({'time': datetime.now(timezone.utc).isoformat()})}\n\n"
                await asyncio.sleep(2.5)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)[:240]}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(4)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/jobs/{job_id}/upload/status")
def upload_status(job_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    job = owned_job(job_id, user_id)
    return {"ok": True, "job_id": job_id, **read_upload_state(safe_job_root(job))}


@router.patch("/api/jobs/{job_id}/meta")
def update_job_meta(job_id: str, payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    job = owned_job(job_id, user_id)
    body = payload if isinstance(payload, dict) else {}
    display_name = str(body.get("display_name") or "").strip()
    if display_name:
        settings = dict(job.get("settings") or {})
        settings["display_name"] = display_name[:255]
        STORE.update_job_input(job_id, settings=settings, user_id=user_id)
    meta = WORKSPACE.update_meta(user_id, job_id, body)
    STORE.add_event(job_id, "Library metadata updated")
    return {"ok": True, "meta": meta}


@router.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, payload: dict[str, Any] | None = None, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    job = owned_job(job_id, user_id)
    if str(job.get("status") or "") not in {"failed", "error", "cancelled"}:
        raise HTTPException(status_code=409, detail="job_not_retryable")
    stage = str((payload or {}).get("stage") or "failed_stage")[:80]
    if not STORE.requeue_job(job_id, user_id=user_id):
        raise HTTPException(status_code=409, detail="retry_not_applied")
    STORE.add_event(job_id, f"User retry requested from stage={stage}")
    WORKSPACE.create_notification(user_id, event_key=f"retry:{job_id}:{time.time_ns()}", kind="job_retry", title="ส่งงานกลับเข้าคิวแล้ว", body="ระบบจะใช้ไฟล์เดิม ไม่ต้องอัปโหลดใหม่", job_id=job_id)
    return {"ok": True, "status": "queued", "reused_upload": True}


@router.post("/api/jobs/{job_id}/duplicate")
def duplicate_job(job_id: str, payload: dict[str, Any] | None = None, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    source_job = owned_job(job_id, user_id)
    source_root = safe_job_root(source_job)
    settings = dict(source_job.get("settings") or {})
    requested = str((payload or {}).get("name") or "").strip()
    settings["display_name"] = requested[:255] if requested else f"{settings.get('display_name') or 'SubCut'} — สำเนา"
    workspace = USER_WORKSPACE_ROOT / str(user_id) / "subcut_jobs"
    workspace.mkdir(parents=True, exist_ok=True)
    root = workspace / f"duplicate-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 10_000_000:07d}"
    root.mkdir(parents=True, exist_ok=False)
    copied = 0
    for path in iter_sources(source_root):
        shutil.copy2(path, root / path.name)
        copied += 1
    for name in (".autosu_only_inputs.json", ".subcut_upload_session.json"):
        source = source_root / name
        if source.exists():
            shutil.copy2(source, root / name)
    if not copied:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=409, detail="source_upload_missing")
    created = STORE.create_job(product_path=str(root.resolve()), mode=str(source_job.get("mode") or "autosu_only"), settings=settings, status="created", user_id=user_id)
    STORE.add_event(created["id"], f"Duplicated from {job_id}; reused_files={copied}")
    WORKSPACE.update_meta(user_id, created["id"], {"display_name": settings["display_name"], "tags": ["สำเนา"]})
    return {"ok": True, "job": created, "reused_files": copied}


@router.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    job = owned_job(job_id, user_id)
    if str(job.get("status") or "") not in TERMINAL:
        raise HTTPException(status_code=409, detail="cancel_job_before_delete")
    root = safe_job_root(job, must_exist=False)
    if not STORE.delete_job(job_id, user_id=user_id):
        raise HTTPException(status_code=409, detail="delete_not_applied")
    shutil.rmtree(root, ignore_errors=True)
    return {"ok": True, "deleted": job_id}


@router.post("/api/jobs/bulk")
def bulk_jobs(payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    ids = [str(value) for value in (payload.get("job_ids") or []) if str(value).strip()][:100]
    action = str(payload.get("action") or "").strip()
    succeeded, failed = [], []
    for job_id in ids:
        try:
            job = owned_job(job_id, user_id)
            if action == "retry":
                ok = STORE.requeue_job(job_id, user_id=user_id)
            elif action == "delete":
                ok = str(job.get("status")) in TERMINAL and STORE.delete_job(job_id, user_id=user_id)
                if ok:
                    shutil.rmtree(safe_job_root(job, must_exist=False), ignore_errors=True)
            elif action in {"pin", "unpin", "favorite", "unfavorite", "move", "tag"}:
                update: dict[str, Any] = {}
                if action in {"pin", "unpin"}: update["pinned"] = action == "pin"
                if action in {"favorite", "unfavorite"}: update["favorite"] = action == "favorite"
                if action == "move": update["folder_id"] = str(payload.get("folder_id") or "")
                if action == "tag": update["tags"] = payload.get("tags") or []
                WORKSPACE.update_meta(user_id, job_id, update); ok = True
            else:
                raise ValueError("unsupported_bulk_action")
            (succeeded if ok else failed).append(job_id)
        except Exception:
            failed.append(job_id)
    return {"ok": not failed, "succeeded": succeeded, "failed": failed}


@router.get("/api/folders")
def folders(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    return {"ok": True, "items": WORKSPACE.list_folders(user_id_from_user(current_user))}


@router.post("/api/folders")
def create_folder(payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    try:
        item = WORKSPACE.create_folder(user_id_from_user(current_user), str(payload.get("name") or ""), str(payload.get("color") or "violet"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail="folder_name_already_exists") from exc
    return {"ok": True, "item": item}


@router.delete("/api/folders/{folder_id}")
def remove_folder(folder_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    return {"ok": WORKSPACE.delete_folder(user_id_from_user(current_user), folder_id)}


@router.get("/api/download-center")
def download_center(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    jobs = [job for job in STORE.list_completed_jobs(limit=500, user_id=user_id) if str(job.get("status")) == "done"]
    metas = WORKSPACE.metadata_map(user_id, [str(job.get("id")) for job in jobs])
    items: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for job in jobs:
        root = safe_job_root(job, must_exist=False)
        meta = metas.get(str(job.get("id")), {})
        finished = datetime.fromisoformat(str(job.get("finished_at") or job.get("updated_at") or now.isoformat()).replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(str(meta.get("retention_until") or (finished + timedelta(days=OUTPUT_RETENTION_DAYS)).isoformat()).replace("Z", "+00:00"))
        files = [file_payload(path, root) for path in iter_outputs(root)] if root.exists() else []
        items.append({
            "job_id": str(job.get("id")), "name": str(meta.get("display_name") or (job.get("settings") or {}).get("display_name") or "SubCut"),
            "finished_at": str(job.get("finished_at") or ""), "expires_at": expiry.isoformat(),
            "expires_in_days": max(0, (expiry - now).days), "files": files,
            "total_bytes": sum(item["size"] for item in files), "favorite": bool(meta.get("favorite")),
        })
    return {"ok": True, "items": items, "storage": storage_stats(user_id)}


@router.post("/api/jobs/{job_id}/retention/extend")
def extend_retention(job_id: str, payload: dict[str, Any] | None = None, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    owned_job(job_id, user_id)
    days = max(1, min(OUTPUT_RETENTION_EXTENSION_MAX_DAYS, int((payload or {}).get("days") or 30)))
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    meta = WORKSPACE.update_meta(user_id, job_id, {"retention_until": until})
    return {"ok": True, "retention_until": meta.get("retention_until")}


def _media_file(job_id: str, user_id: int, path: Path, root: Path) -> FileResponse:
    target = path.resolve()
    if target != root and root not in target.parents or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="media_not_found")
    return FileResponse(str(target), filename=target.name)


@router.get("/api/jobs/{job_id}/media/source/{index}")
def source_media(job_id: str, index: int, current_user: dict = Depends(require_request_user)) -> FileResponse:
    user_id = user_id_from_user(current_user)
    root = safe_job_root(owned_job(job_id, user_id))
    files = iter_sources(root)
    if index < 0 or index >= len(files):
        raise HTTPException(status_code=404, detail="source_not_found")
    return _media_file(job_id, user_id, files[index], root)


@router.get("/api/jobs/{job_id}/media/output/{file_path:path}")
def output_media(job_id: str, file_path: str, current_user: dict = Depends(require_request_user)) -> FileResponse:
    user_id = user_id_from_user(current_user)
    root = safe_job_root(owned_job(job_id, user_id))
    output_root = (root / "output").resolve()
    relative = str(file_path or "").replace("\\", "/").lstrip("/")
    if relative.startswith("output/"):
        relative = relative[7:]
    return _media_file(job_id, user_id, output_root / relative, output_root)


@router.get("/api/jobs/{job_id}/waveform")
def waveform(job_id: str, source: str = Query(default="source", pattern="^(source|output)$"),
             file_path: str = Query(default=""), current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    root = safe_job_root(owned_job(job_id, user_id))
    if source == "source":
        candidates = iter_sources(root)
        target = candidates[0] if candidates else None
    else:
        output_root = (root / "output").resolve()
        relative = str(file_path or "").replace("\\", "/").lstrip("/")
        if relative.startswith("output/"):
            relative = relative[7:]
        candidate = (output_root / relative).resolve() if relative else None
        target = candidate if candidate and output_root in candidate.parents else None
    if not target or not target.exists():
        raise HTTPException(status_code=404, detail="waveform_media_not_found")
    command = ["ffmpeg", "-v", "error", "-i", str(target), "-t", "1200", "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "-"]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"waveform_failed:{str(exc)[:120]}") from exc
    samples = array("h")
    samples.frombytes(result.stdout)
    bucket_count = 320
    stride = max(1, len(samples) // bucket_count)
    peaks = []
    for index in range(0, len(samples), stride):
        block = samples[index:index + stride]
        peaks.append(round(max((abs(value) for value in block), default=0) / 32768, 4))
        if len(peaks) >= bucket_count:
            break
    return {"ok": True, "peaks": peaks, "sample_rate": 4000, "duration_cap_sec": 1200}
