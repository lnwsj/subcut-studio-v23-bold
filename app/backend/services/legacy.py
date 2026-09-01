

"""Compatibility serializers consumed by the new SubCut frontend."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .job_store import JobStore


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def legacy_status(status: str) -> str:
    return "error" if str(status).lower() == "failed" else str(status)


def legacy_progress(job: dict[str, Any]) -> int:
    status = str(job.get("status") or "").lower()
    outcome = job.get("outcome") if isinstance(job.get("outcome"), dict) else {}
    scan = job.get("scan_summary") if isinstance(job.get("scan_summary"), dict) else {}
    for source, ceiling in ((outcome, 100), (scan, 99)):
        for key in ("progress", "progress_percent"):
            if key in source:
                return max(0, min(ceiling, _safe_int(source.get(key))))
    if status == "done":
        return 100
    if status == "running":
        return 50
    return 0


def job_result_payload(job: dict[str, Any]) -> dict[str, Any]:
    outcome = dict(job.get("outcome") or {})
    settings = dict(job.get("settings") or {})
    autosu = dict(outcome.get("autosu") or {})
    runtime_metrics = dict(outcome.get("runtime_metrics") or {})
    outputs = list(outcome.get("outputs") or [])
    outputs_with_sub = list(outcome.get("outputs_with_sub") or autosu.get("outputs") or [])
    output_count = _safe_int(outcome.get("output_count"), len(outputs))
    autosu_succeeded = _safe_int(
        outcome.get("autosu_succeeded", autosu.get("succeeded")),
        len(outputs_with_sub),
    )
    autosu_failed = _safe_int(
        outcome.get("autosu_failed", runtime_metrics.get("failed_subtitles", autosu.get("failed"))),
        0,
    )
    used_encoder = str(
        outcome.get("used_encoder")
        or autosu.get("used_encoder")
        or settings.get("encoder")
        or ""
    )
    return {
        "output_count": output_count,
        "mode": str(outcome.get("mode_label") or outcome.get("mode_key") or job.get("mode") or ""),
        "encoder": used_encoder or "libx264",
        "used_encoder": used_encoder,
        "elapsed": str(outcome.get("elapsed_str") or ""),
        "outputs": outputs,
        "runtime_metrics": runtime_metrics,
        "autosu_requested": bool(autosu.get("requested", str(job.get("mode")) == "autosu_only")),
        "autosu_applied": bool(autosu.get("applied", autosu_succeeded > 0)),
        "autosu_output_count": autosu_succeeded,
        "autosu_attempted": _safe_int(autosu.get("attempted"), autosu_succeeded + autosu_failed),
        "autosu_succeeded": autosu_succeeded,
        "autosu_failed": autosu_failed,
        "autosu_partial": bool(autosu_succeeded > 0 and autosu_failed > 0),
        "autosu_items": list(autosu.get("items") or []),
        "autosu_errors": list(autosu.get("errors") or []),
        "outputs_with_sub": outputs_with_sub,
        "failed_subtitles": autosu_failed,
        "autosu": autosu,
    }


def job_to_legacy(job: dict[str, Any], store: "JobStore", *, include_details: bool = False) -> dict[str, Any]:
    product_path = str(job.get("product_path") or "")
    settings = dict(job.get("settings") or {})
    name = str(settings.get("display_name") or "").strip() or Path(product_path).name or str(job.get("id") or "")[:8]
    # Pull live stage info from scan_summary (set by worker via update_scan_summary)
    scan = dict(job.get("scan_summary") or {})
    payload: dict[str, Any] = {
        "id": str(job.get("id") or ""),
        "name": name,
        "product_path": product_path,
        "mode": str(job.get("mode") or ""),
        "status": legacy_status(str(job.get("status") or "")),
        "progress": legacy_progress(job),
        "stage": str(scan.get("stage") or ""),
        "current": int(scan.get("current") or 0),
        "total": int(scan.get("total") or 0),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
        "error": str(job.get("error") or ""),
        "settings": settings,
        "result": job_result_payload(job),
    }
    if include_details:
        payload["logs"] = [
            str(item.get("message") or "")
            for item in store.get_events(str(job.get("id") or ""), limit=500)
        ]
    return payload