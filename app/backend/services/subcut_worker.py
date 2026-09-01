

"""Background worker dedicated to SJ88 SubCut Studio jobs."""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import JOB_HEARTBEAT_INTERVAL_SEC, JOB_LEASE_TTL_SEC
from .autosu_runner import get_ffmpeg_path, run_autosu_on_outputs
from .autosu_settings import AutoSuSettings, load_autosu_settings
from .constants import VIDEO_EXTS
from .job_store import JobStore
from .subtitle_trim_engine import trim_silence_proxy
from .subtitle_trim_settings import build_runtime_subtitle_trim_settings


logger = logging.getLogger("sj88.subcut.worker")
_INPUT_MANIFEST = ".autosu_only_inputs.json"
_SUBCUT_MODES = ("autosu_only", "silence_trim_only")


class _LeaseHeartbeat(threading.Thread):
    """Renew a job lease while one processing attempt is active."""

    def __init__(
        self,
        *,
        store: JobStore,
        job_id: str,
        worker_id: str,
        run_token: str,
        interval_sec: float,
        ttl_sec: int,
    ) -> None:
        super().__init__(name=f"subcut-lease-{job_id[:8]}", daemon=True)
        self.store = store
        self.job_id = job_id
        self.worker_id = worker_id
        self.run_token = run_token
        self.interval_sec = max(1.0, float(interval_sec))
        self.ttl_sec = max(30, int(ttl_sec))
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_sec):
            try:
                renewed = self.store.renew_job_lease(
                    self.job_id,
                    worker_id=self.worker_id,
                    run_token=self.run_token,
                    lease_ttl_sec=self.ttl_sec,
                )
            except Exception:
                logger.exception("SubCut lease renewal failed job=%s", self.job_id)
                renewed = False
            if not renewed:
                self.lost_event.set()
                return

    def stop(self) -> None:
        self.stop_event.set()


class SubCutWorker(threading.Thread):
    """Claim and process subtitle/silence jobs without legacy editor imports."""

    def __init__(
        self,
        store: JobStore,
        *,
        poll_interval: float = 1.0,
        worker_id: str | None = None,
    ) -> None:
        resolved_id = str(worker_id or f"subcut-worker:{uuid.uuid4().hex[:8]}")[:128]
        super().__init__(name=resolved_id.replace(":", "-"), daemon=True)
        self.store = store
        self.poll_interval = max(0.2, float(poll_interval))
        self.worker_id = resolved_id
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        logger.info("SubCut worker started id=%s", self.worker_id)
        while not self.stop_event.is_set():
            run_token = uuid.uuid4().hex
            try:
                job = self.store.claim_next_queued_job(
                    worker_id=self.worker_id,
                    run_token=run_token,
                    lease_ttl_sec=JOB_LEASE_TTL_SEC,
                    modes=_SUBCUT_MODES,
                )
            except Exception:
                logger.exception("SubCut queue claim failed worker=%s", self.worker_id)
                self.stop_event.wait(self.poll_interval)
                continue
            if not job:
                self.stop_event.wait(self.poll_interval)
                continue

            job_id = str(job.get("id") or "")
            token = str(job.get("run_token") or run_token)
            lease = _LeaseHeartbeat(
                store=self.store,
                job_id=job_id,
                worker_id=self.worker_id,
                run_token=token,
                interval_sec=JOB_HEARTBEAT_INTERVAL_SEC,
                ttl_sec=JOB_LEASE_TTL_SEC,
            )
            lease.start()
            try:
                self._process_job(job, run_token=token, lease=lease)
            except Exception as exc:
                logger.exception("SubCut job crashed job=%s", job_id)
                self._event(job_id, f"Worker exception: {exc}", level="error", run_token=token)
                self.store.finish_job(
                    job_id,
                    status="failed",
                    outcome={},
                    error=str(exc)[:2000],
                    total_outputs=0,
                    total_output_bytes=0,
                    run_token=token,
                    failure_code="worker_exception",
                )
            finally:
                lease.stop()
                lease.join(timeout=2.0)
        logger.info("SubCut worker stopped id=%s", self.worker_id)

    def _event(self, job_id: str, message: str, *, level: str = "info", run_token: str) -> None:
        try:
            self.store.add_event(job_id, str(message), level=level, run_token=run_token)
        except Exception:
            logger.exception("Failed to write SubCut event job=%s", job_id)

    def _progress(
        self,
        job_id: str,
        *,
        percent: int,
        stage: str,
        run_token: str,
        current: int = 0,
        total: int = 0,
    ) -> None:
        payload = {
            "progress": max(0, min(99, int(percent))),
            "stage": str(stage)[:160],
            "current": max(0, int(current)),
            "total": max(0, int(total)),
        }
        try:
            self.store.update_scan_summary(job_id, payload, run_token=run_token)
        except Exception:
            logger.exception("Failed to update SubCut progress job=%s", job_id)

    @staticmethod
    def _sum_bytes(paths: list[Path]) -> int:
        total = 0
        for path in paths:
            try:
                total += int(path.stat().st_size)
            except Exception:
                continue
        return total

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        candidate = path.resolve()
        base = root.resolve()
        return candidate == base or base in candidate.parents

    def _collect_sources(self, root: Path) -> list[Path]:
        import json

        manifest_path = root / _INPUT_MANIFEST
        candidates: list[Path] = []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = payload.get("files") if isinstance(payload, dict) else []
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("relpath") or "").strip()
                if relative:
                    candidates.append((root / relative).resolve())
        except Exception:
            candidates = []

        if not candidates:
            for item in sorted(root.iterdir()):
                if item.is_file() and not item.name.startswith(".") and item.suffix.lower() in VIDEO_EXTS:
                    candidates.append(item.resolve())

        output: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            key = str(item).lower()
            if key in seen:
                continue
            if not self._inside(item, root) or not item.exists() or not item.is_file():
                continue
            if item.suffix.lower() not in VIDEO_EXTS:
                continue
            seen.add(key)
            output.append(item)
        return output

    @staticmethod
    def _clean_run_directories(root: Path) -> tuple[Path, Path]:
        output_root = root / "output"
        work_root = root / ".silence_trim_work"
        for path in (output_root, work_root):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        output_root.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)
        return output_root, work_root

    @staticmethod
    def _apply_autosu_job_overrides(
        settings: AutoSuSettings,
        raw_settings: dict[str, Any] | None,
    ) -> AutoSuSettings:
        if not isinstance(raw_settings, dict):
            return settings
        payload = settings.to_dict()
        mapping = {
            "subtitle_template_id": "subtitle_template_id",
            "subtitle_language": "language",
            "subtitle_position_percent": "position_percent",
            "subtitle_font_size": "font_size",
            "subtitle_font_color": "font_color",
            "subtitle_border_color": "border_color",
            "subtitle_background_color": "background_color",
            "subtitle_background_opacity": "background_opacity",
            "subtitle_max_words_per_line": "max_words_per_line",
            "subtitle_max_syllables_per_line": "max_syllables_per_line",
        }
        for source_key, target_key in mapping.items():
            if source_key in raw_settings and raw_settings[source_key] is not None:
                payload[target_key] = raw_settings[source_key]
        payload["enabled"] = True
        return AutoSuSettings.from_dict(payload)

    def _cancel_check(
        self,
        job_id: str,
        *,
        run_token: str,
        lease: _LeaseHeartbeat,
    ) -> bool:
        if lease.lost_event.is_set():
            return True
        try:
            return self.store.is_cancel_requested(job_id, run_token=run_token)
        except Exception:
            lease.lost_event.set()
            return True

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        outcome: dict[str, Any],
        error: str,
        total_input_bytes: int,
        total_output_bytes: int,
        total_outputs: int,
        run_token: str,
        failure_code: str = "",
    ) -> None:
        finished = self.store.finish_job(
            job_id,
            status=status,
            outcome=outcome,
            error=error,
            total_input_bytes=total_input_bytes,
            total_output_bytes=total_output_bytes,
            total_outputs=total_outputs,
            run_token=run_token,
            failure_code=failure_code,
        )
        if not finished:
            logger.warning("SubCut finish rejected by lease fence job=%s status=%s", job_id, status)

    def _process_job(self, job: dict[str, Any], *, run_token: str, lease: _LeaseHeartbeat) -> None:
        job_id = str(job.get("id") or "")
        mode = str(job.get("mode") or "").strip().lower()
        root = Path(str(job.get("product_path") or "")).resolve()
        settings = dict(job.get("settings") or {})
        user_id = int(job.get("user_id") or 0)

        self._event(job_id, f"Worker picked job: mode={mode}", run_token=run_token)
        self._progress(job_id, percent=3, stage="กำลังตรวจสอบไฟล์", run_token=run_token)
        if not root.exists() or not root.is_dir():
            self._finish(
                job_id,
                status="failed",
                outcome={},
                error="Job workspace does not exist",
                total_input_bytes=0,
                total_output_bytes=0,
                total_outputs=0,
                run_token=run_token,
                failure_code="workspace_missing",
            )
            return

        sources = self._collect_sources(root)
        if not sources:
            self._event(job_id, "No uploaded video files found", level="error", run_token=run_token)
            self._finish(
                job_id,
                status="failed",
                outcome={},
                error="No uploaded video files found",
                total_input_bytes=0,
                total_output_bytes=0,
                total_outputs=0,
                run_token=run_token,
                failure_code="input_missing",
            )
            return

        total_input_bytes = self._sum_bytes(sources)
        if self._cancel_check(job_id, run_token=run_token, lease=lease):
            self._finish(
                job_id,
                status="cancelled",
                outcome={},
                error="Cancelled before processing",
                total_input_bytes=total_input_bytes,
                total_output_bytes=0,
                total_outputs=0,
                run_token=run_token,
            )
            return

        if mode == "silence_trim_only":
            self._process_silence(
                job_id=job_id,
                root=root,
                sources=sources,
                settings=settings,
                user_id=user_id,
                total_input_bytes=total_input_bytes,
                run_token=run_token,
                lease=lease,
            )
            return
        if mode == "autosu_only":
            self._process_autosu(
                job_id=job_id,
                root=root,
                sources=sources,
                settings=settings,
                user_id=user_id,
                total_input_bytes=total_input_bytes,
                run_token=run_token,
                lease=lease,
            )
            return
        self._finish(
            job_id,
            status="failed",
            outcome={},
            error=f"Unsupported mode: {mode}",
            total_input_bytes=total_input_bytes,
            total_output_bytes=0,
            total_outputs=0,
            run_token=run_token,
            failure_code="unsupported_mode",
        )

    def _process_silence(
        self,
        *,
        job_id: str,
        root: Path,
        sources: list[Path],
        settings: dict[str, Any],
        user_id: int,
        total_input_bytes: int,
        run_token: str,
        lease: _LeaseHeartbeat,
    ) -> None:
        output_root, work_root = self._clean_run_directories(root)
        merged = dict(settings)
        merged["trim_silence"] = True
        trim_settings = build_runtime_subtitle_trim_settings(user_id=user_id, job_settings=merged)
        trim_settings.trim_silence = True
        ffmpeg_exe = str(get_ffmpeg_path() or "ffmpeg")
        self._event(
            job_id,
            (
                f"[SilenceCut] start files={len(sources)} "
                f"threshold={trim_settings.trim_silence_threshold_db}dB "
                f"min_silence={trim_settings.trim_silence_min_silence_sec}s"
            ),
            run_token=run_token,
        )

        started = time.monotonic()
        outputs: list[str] = []
        manifests: list[dict[str, Any]] = []
        errors: list[str] = []
        applied_count = 0
        skipped_count = 0
        used_names: set[str] = set()

        for index, source in enumerate(sources, start=1):
            if self._cancel_check(job_id, run_token=run_token, lease=lease):
                break
            progress = 8 + int(((index - 1) / max(1, len(sources))) * 86)
            self._progress(
                job_id,
                percent=progress,
                stage=f"กำลังตัดเสียงเงียบ {index}/{len(sources)}",
                run_token=run_token,
                current=index,
                total=len(sources),
            )
            self._event(job_id, f"[SilenceCut] {index}/{len(sources)} {source.name}", run_token=run_token)
            safe_stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in source.stem)[:96]
            safe_stem = safe_stem.strip("_-") or f"video_{index}"
            try:
                processed, manifest = trim_silence_proxy(
                    source,
                    work_root / f"{index:04d}_{safe_stem}",
                    trim_settings,
                    lambda message: self._event(job_id, message, run_token=run_token),
                    ffmpeg_exe=ffmpeg_exe,
                )
                if self._cancel_check(job_id, run_token=run_token, lease=lease):
                    break
                item = dict(manifest or {})
                # Applied trims are normalized to MP4 by the engine. When no
                # silence is found, keep the original container instead of
                # copying WebM/MKV bytes into a misleading .mp4 filename.
                suffix = ".mp4" if not bool(item.get("skipped", True)) else (processed.suffix.lower() or source.suffix.lower() or ".mp4")
                name = f"{safe_stem}_cut{suffix}"
                sequence = 2
                while name.lower() in used_names:
                    name = f"{safe_stem}_cut_{sequence}{suffix}"
                    sequence += 1
                used_names.add(name.lower())
                destination = output_root / name
                shutil.copy2(processed, destination)
                item["delivered_output"] = str(destination)
                manifests.append(item)
                if bool(item.get("skipped", True)):
                    skipped_count += 1
                else:
                    applied_count += 1
                outputs.append(str(destination))
            except Exception as exc:
                message = f"{source.name}: {exc}"
                errors.append(message)
                self._event(job_id, f"[SilenceCut] failed {message}", level="error", run_token=run_token)

        elapsed = self._format_elapsed(time.monotonic() - started)
        output_paths = [Path(path) for path in outputs]
        total_output_bytes = self._sum_bytes(output_paths)
        before_duration = sum(float(item.get("before_duration") or 0.0) for item in manifests)
        after_duration = sum(
            float(item.get("after_duration") or item.get("before_duration") or 0.0)
            for item in manifests
        )
        removed_duration = sum(float(item.get("removed_duration") or 0.0) for item in manifests)
        cancelled = self._cancel_check(job_id, run_token=run_token, lease=lease)
        payload = {
            "ok": bool(outputs),
            "output_count": len(outputs),
            "outputs": outputs,
            "errors": errors[:20],
            "elapsed_str": elapsed,
            "mode_key": "silence_trim_only",
            "mode_label": "Silence Cutter",
            "initial_encoder": "libx264",
            "used_encoder": "libx264",
            "encoder_fallback_used": False,
            "progress": 100 if outputs and not cancelled else 0,
            "runtime_metrics": {
                "source_video_count": len(sources),
                "processed_video_count": len(outputs),
                "trim_applied_count": applied_count,
                "trim_skipped_count": skipped_count,
                "before_duration_sec": round(before_duration, 3),
                "after_duration_sec": round(after_duration, 3),
                "removed_duration_sec": round(removed_duration, 3),
            },
            "clip_metrics": manifests,
            "silence_trim": {
                "requested": True,
                "applied_count": applied_count,
                "skipped_count": skipped_count,
                "settings": trim_settings.to_dict(),
                "items": manifests,
            },
        }

        if cancelled:
            self._event(job_id, f"[SilenceCut] cancelled outputs={len(outputs)}", level="warn", run_token=run_token)
            self._finish(
                job_id,
                status="cancelled",
                outcome=payload,
                error="Cancelled by user",
                total_input_bytes=total_input_bytes,
                total_output_bytes=total_output_bytes,
                total_outputs=len(outputs),
                run_token=run_token,
            )
            return
        if outputs:
            level = "warn" if errors else "info"
            self._event(
                job_id,
                f"[SilenceCut] completed outputs={len(outputs)} removed={removed_duration:.2f}s errors={len(errors)}",
                level=level,
                run_token=run_token,
            )
            self._finish(
                job_id,
                status="done",
                outcome=payload,
                error="",
                total_input_bytes=total_input_bytes,
                total_output_bytes=total_output_bytes,
                total_outputs=len(outputs),
                run_token=run_token,
            )
            return
        error = " | ".join(errors[:3]) or "Silence trim failed"
        self._finish(
            job_id,
            status="failed",
            outcome=payload,
            error=error,
            total_input_bytes=total_input_bytes,
            total_output_bytes=0,
            total_outputs=0,
            run_token=run_token,
            failure_code="silence_trim_failed",
        )

    def _process_autosu(
        self,
        *,
        job_id: str,
        root: Path,
        sources: list[Path],
        settings: dict[str, Any],
        user_id: int,
        total_input_bytes: int,
        run_token: str,
        lease: _LeaseHeartbeat,
    ) -> None:
        output_root, _work_root = self._clean_run_directories(root)
        autosu = self._apply_autosu_job_overrides(load_autosu_settings(), settings)
        trim_settings = build_runtime_subtitle_trim_settings(user_id=user_id, job_settings=settings)
        workflow = str(settings.get("workflow") or "subtitle")
        self._event(
            job_id,
            (
                f"[AutoSu] start files={len(sources)} language={autosu.language} "
                f"template={autosu.subtitle_template_id} trim={bool(trim_settings.trim_silence)}"
            ),
            run_token=run_token,
        )
        self._progress(job_id, percent=8, stage="กำลังเตรียมโมเดลซับ", run_token=run_token)
        started = time.monotonic()

        def cancel_check() -> bool:
            return self._cancel_check(job_id, run_token=run_token, lease=lease)

        def progress_callback(index: int, total: int, source: str) -> None:
            percent = 12 + int(((max(1, index) - 1) / max(1, total)) * 82)
            self._progress(
                job_id,
                percent=percent,
                stage=f"กำลังทำซับ {index}/{total}",
                run_token=run_token,
                current=index,
                total=total,
            )

        # Track base percent for the file (12..94)
        file_base_percent = {"value": 12}
        file_total = max(1, len(sources))

        def status_callback(stage: str) -> None:
            """Update detailed status (Thai stage text) for the current file."""
            try:
                idx = sources.index(sources[0]) + 1  # always 1 for single-file case
            except Exception:
                idx = 1
            # Keep percent in 15..90 range while stage messages flow
            self._progress(
                job_id,
                percent=15,
                stage=stage,
                run_token=run_token,
                current=idx,
                total=file_total,
            )

        run_result = run_autosu_on_outputs(
            [str(path) for path in sources],
            autosu,
            output_root=output_root,
            log_cb=lambda message: self._event(job_id, message, run_token=run_token),
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            status_callback=status_callback,
            trim_settings=trim_settings,
        )
        shutil.rmtree(output_root / "withsub" / "trim_work", ignore_errors=True)

        outputs = [str(path) for path in (run_result.get("outputs") or []) if str(path).strip()]
        output_paths = [Path(path) for path in outputs if Path(path).exists()]
        total_output_bytes = self._sum_bytes(output_paths)
        succeeded = int(run_result.get("succeeded") or len(output_paths))
        attempted = int(run_result.get("attempted") or len(sources))
        failed = int(run_result.get("failed") or max(0, attempted - succeeded))
        errors = [str(item) for item in (run_result.get("errors") or []) if str(item).strip()]
        trim_manifests = [
            dict(item)
            for item in (run_result.get("trim_manifests") or [])
            if isinstance(item, dict)
        ]
        removed_duration = sum(float(item.get("removed_duration") or 0.0) for item in trim_manifests)
        before_duration = sum(float(item.get("before_duration") or 0.0) for item in trim_manifests)
        after_duration = sum(
            float(item.get("after_duration") or item.get("before_duration") or 0.0)
            for item in trim_manifests
        )
        cancelled = cancel_check()
        initial_encoder = str(run_result.get("initial_encoder") or "libx264")
        used_encoder = str(run_result.get("used_encoder") or initial_encoder)
        elapsed = self._format_elapsed(time.monotonic() - started)
        payload = {
            "ok": succeeded > 0,
            "output_count": len(output_paths),
            "outputs": [str(path) for path in output_paths],
            "errors": errors[:20],
            "autosu_attempted": attempted,
            "autosu_succeeded": succeeded,
            "autosu_failed": failed,
            "autosu_partial": bool(succeeded > 0 and failed > 0),
            "elapsed_str": elapsed,
            "mode_key": "autosu_only",
            "mode_label": "Subtitle + Silence" if workflow == "combined" else "Auto Subtitle",
            "initial_encoder": initial_encoder,
            "used_encoder": used_encoder,
            "encoder_fallback_used": bool(run_result.get("encoder_fallback_used", used_encoder != initial_encoder)),
            "progress": 100 if succeeded > 0 and not cancelled else 0,
            "runtime_metrics": {
                "source_video_count": len(sources),
                "attempted_subtitles": attempted,
                "succeeded_subtitles": succeeded,
                "failed_subtitles": failed,
                "trim_applied_count": int(run_result.get("trim_applied_count") or 0),
                "before_duration_sec": round(before_duration, 3),
                "after_duration_sec": round(after_duration, 3),
                "removed_duration_sec": round(removed_duration, 3),
            },
            "clip_metrics": list(run_result.get("items") or []),
            "autosu": {"requested": True, "applied": succeeded > 0, **run_result},
        }

        if cancelled:
            self._event(job_id, f"[AutoSu] cancelled outputs={len(output_paths)}", level="warn", run_token=run_token)
            self._finish(
                job_id,
                status="cancelled",
                outcome=payload,
                error="Cancelled by user",
                total_input_bytes=total_input_bytes,
                total_output_bytes=total_output_bytes,
                total_outputs=len(output_paths),
                run_token=run_token,
            )
            return
        if succeeded > 0:
            level = "warn" if failed else "info"
            self._event(
                job_id,
                f"[AutoSu] completed succeeded={succeeded} failed={failed} attempted={attempted}",
                level=level,
                run_token=run_token,
            )
            self._finish(
                job_id,
                status="done",
                outcome=payload,
                error="",
                total_input_bytes=total_input_bytes,
                total_output_bytes=total_output_bytes,
                total_outputs=len(output_paths),
                run_token=run_token,
            )
            return
        error = " | ".join(errors[:3]) or "Auto subtitle failed"
        self._event(job_id, f"[AutoSu] failed: {error}", level="error", run_token=run_token)
        self._finish(
            job_id,
            status="failed",
            outcome=payload,
            error=error,
            total_input_bytes=total_input_bytes,
            total_output_bytes=0,
            total_outputs=0,
            run_token=run_token,
            failure_code="autosu_failed",
        )