"""Job store - SQLite persistence layer.

This module has been refactored:
- db.py: Database connection and schema
- queries.py: Job CRUD operations
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import init_schema, row_to_job, _connect as db_connect, _decode_json, is_mysql_backend
from .queries import JobQueries


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


INPUT_CLEANUP_DIR_NAMES = ("vdo_long", "vdo_ai", "audio", "a", "b", "Process")


def _is_under_allowed_root(path: Path, allowed_roots: Iterable[str | Path]) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for raw_root in allowed_roots:
        if not raw_root:
            continue
        try:
            resolved.relative_to(Path(raw_root).resolve())
            return True
        except Exception:
            continue
    return False


class JobStore:
    """Persistent job/event store backed by SQLite.

    This class is a compatibility wrapper around JobQueries.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if not is_mysql_backend():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = __import__("threading").Lock()
        self._queries = JobQueries(db_path)

    def _connect(self):
        return db_connect(self.db_path)

    def _init_schema(self) -> None:
        init_schema(self.db_path)

    @staticmethod
    def _decode_json(text: str) -> dict[str, Any]:
        return _decode_json(text)

    def _row_to_job(self, row):
        return row_to_job(row)

    def create_job(
        self,
        *,
        product_path: str,
        mode: str,
        settings: dict[str, Any] | None = None,
        status: str = "queued",
        user_id: int = 0,
    ) -> dict[str, Any]:
        return self._queries.create_job(
            product_path=product_path,
            mode=mode,
            settings=settings,
            status=status,
            user_id=user_id,
        )

    def get_job(self, job_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
        return self._queries.get_job(job_id, user_id=user_id)

    def list_jobs(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._queries.list_jobs(limit=limit, status=status, user_id=user_id)

    def add_event(
        self,
        job_id: str,
        message: str,
        level: str = "info",
        *,
        run_token: str | None = None,
    ) -> int:
        return self._queries.add_event(job_id, message, level, run_token=run_token)

    def get_events(self, job_id: str, *, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return self._queries.get_events(job_id, after_id=after_id, limit=limit)

    def request_cancel(self, job_id: str, *, user_id: int | None = None) -> bool:
        return self._queries.request_cancel(job_id, user_id=user_id)

    def is_cancel_requested(self, job_id: str, *, run_token: str | None = None) -> bool:
        return self._queries.is_cancel_requested(job_id, run_token=run_token)

    def claim_next_queued_job(
        self,
        *,
        worker_id: str = "legacy-worker",
        run_token: str | None = None,
        lease_ttl_sec: int = 90,
        modes: tuple[str, ...] | None = None,
        exclude_modes: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        return self._queries.claim_next_queued_job(
            worker_id=worker_id,
            run_token=run_token,
            lease_ttl_sec=lease_ttl_sec,
            modes=modes,
            exclude_modes=exclude_modes,
        )

    def renew_job_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        run_token: str,
        lease_ttl_sec: int = 90,
    ) -> bool:
        return self._queries.renew_job_lease(
            job_id,
            worker_id=worker_id,
            run_token=run_token,
            lease_ttl_sec=lease_ttl_sec,
        )

    def release_claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        run_token: str,
        reason: str = "worker_draining",
    ) -> bool:
        return self._queries.release_claim(
            job_id,
            worker_id=worker_id,
            run_token=run_token,
            reason=reason,
        )

    def retry_running_job(self, job_id: str, *, run_token: str) -> bool:
        return self._queries.retry_running_job(job_id, run_token=run_token)

    def has_queued_jobs(self) -> bool:
        return self._queries.has_queued_jobs()

    def get_queue_load(self, *, user_id: int | None = None) -> dict[str, int]:
        return self._queries.get_queue_load(user_id=user_id)

    def get_job_metrics_rows(self, *, limit: int = 2000, user_id: int | None = None) -> list[dict[str, Any]]:
        return self._queries.get_job_metrics_rows(limit=limit, user_id=user_id)

    def get_recent_incident_events(self, *, limit: int = 50, user_id: int | None = None) -> list[dict[str, Any]]:
        return self._queries.get_recent_incident_events(limit=limit, user_id=user_id)

    def update_scan_summary(
        self,
        job_id: str,
        scan_summary: dict[str, Any],
        *,
        run_token: str | None = None,
    ) -> bool:
        return self._queries.update_scan_summary(job_id, scan_summary, run_token=run_token)

    def update_job_input(
        self,
        job_id: str,
        *,
        product_path: str | None = None,
        settings: dict[str, Any] | None = None,
        user_id: int | None = None,
        run_token: str | None = None,
    ) -> bool:
        return self._queries.update_job_input(
            job_id,
            product_path=product_path,
            settings=settings,
            user_id=user_id,
            run_token=run_token,
        )

    def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        clear_error: bool = False,
        reset_run_state: bool = False,
        user_id: int | None = None,
    ) -> bool:
        return self._queries.set_job_status(
            job_id,
            status,
            clear_error=clear_error,
            reset_run_state=reset_run_state,
            user_id=user_id,
        )

    def queue_job(self, job_id: str, *, user_id: int | None = None) -> bool:
        return self._queries.queue_job(job_id, user_id=user_id)

    def list_completed_jobs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        mode: str | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._queries.list_completed_jobs(limit=limit, offset=offset, mode=mode, user_id=user_id)

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        outcome: dict[str, Any] | None = None,
        error: str = "",
        total_input_bytes: int = 0,
        total_output_bytes: int = 0,
        total_outputs: int = 0,
        run_token: str | None = None,
        failure_code: str = "",
    ) -> bool:
        return self._queries.finish_job(
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

    def cancel_queued_job(self, job_id: str, *, user_id: int | None = None) -> bool:
        return self._queries.cancel_queued_job(job_id, user_id=user_id)

    def requeue_job(
        self,
        job_id: str,
        *,
        user_id: int | None = None,
        run_token: str | None = None,
    ) -> bool:
        return self._queries.requeue_job(job_id, user_id=user_id, run_token=run_token)

    def recover_stale_jobs(self, *, stale_minutes: int = 30) -> list[str]:
        return self._queries.recover_stale_jobs(stale_minutes=stale_minutes)

    def recover_expired_leases(self) -> list[str]:
        return self._queries.recover_expired_leases()

    def cleanup_expired_outputs(
        self,
        *,
        retention_minutes: int = 30,
        batch_limit: int = 200,
    ) -> dict[str, int]:
        """Remove per-job output folders after retention window.

        This only deletes `<product_path>/output` and leaves inputs/log metadata intact.
        """
        retention = max(0, int(retention_minutes))
        if retention <= 0:
            return {
                "jobs_scanned": 0,
                "jobs_marked": 0,
                "output_dirs_removed": 0,
                "files_removed": 0,
            }

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=retention)).isoformat()
        now = _utc_now()
        scan_limit = max(1, int(batch_limit))

        jobs_scanned = 0
        jobs_marked = 0
        output_dirs_removed = 0
        files_removed = 0

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, product_path, outcome_json
                FROM jobs
                WHERE status IN ('done','failed','error','cancelled')
                  AND product_path <> ''
                  AND (
                    (finished_at <> '' AND finished_at <= ?)
                    OR (finished_at = '' AND created_at <= ?)
                  )
                ORDER BY finished_at ASC, created_at ASC
                LIMIT ?
                """,
                (cutoff, cutoff, scan_limit),
            ).fetchall()

            for row in rows:
                jobs_scanned += 1
                data = row if isinstance(row, dict) else dict(row)
                job_id = str(data.get("id") or "").strip()
                product_path = str(data.get("product_path") or "").strip()
                if not job_id or not product_path:
                    continue

                outcome = self._decode_json(data.get("outcome_json") or "{}")
                if str(outcome.get("__output_pruned_at") or "").strip():
                    continue

                output_dir = (Path(product_path) / "output")
                removed_here = 0
                dir_removed = False

                if output_dir.exists() and output_dir.is_dir():
                    try:
                        removed_here = sum(1 for item in output_dir.rglob("*") if item.is_file())
                    except Exception:
                        removed_here = 0
                    try:
                        shutil.rmtree(output_dir)
                        dir_removed = True
                    except Exception:
                        dir_removed = False

                outcome["__output_pruned_at"] = now
                outcome["__output_retention_minutes"] = retention
                outcome["__output_pruned_ok"] = bool(dir_removed or not output_dir.exists())
                conn.execute(
                    "UPDATE jobs SET outcome_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(outcome, ensure_ascii=False), now, job_id),
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        now,
                        "info",
                        f"Output auto-cleanup: retention={retention}m removed_output_dir={int(dir_removed)} files_removed={removed_here}",
                    ),
                )

                jobs_marked += 1
                files_removed += max(0, int(removed_here))
                if dir_removed:
                    output_dirs_removed += 1

        return {
            "jobs_scanned": jobs_scanned,
            "jobs_marked": jobs_marked,
            "output_dirs_removed": output_dirs_removed,
            "files_removed": files_removed,
        }

    def cleanup_expired_inputs(
        self,
        *,
        retention_hours: float = 24.0,
        failed_retention_hours: float = 72.0,
        batch_limit: int = 200,
        allowed_roots: Iterable[str | Path] = (),
    ) -> dict[str, int]:
        """Remove retained input folders after a job is safely past its retention window.

        Only top-level input folders are removed. Output, metadata, reports, and DB records
        stay intact. Paths outside the explicit allowed roots are skipped to avoid pruning
        migrated production/V4 paths that may still be referenced by V2 data.
        """
        done_retention = max(0.0, float(retention_hours))
        failed_retention = max(0.0, float(failed_retention_hours))
        if done_retention <= 0 and failed_retention <= 0:
            return {
                "jobs_scanned": 0,
                "jobs_marked": 0,
                "dirs_removed": 0,
                "files_removed": 0,
                "bytes_removed": 0,
                "paths_skipped": 0,
            }

        safe_roots = tuple(Path(root) for root in allowed_roots if str(root or "").strip())
        if not safe_roots:
            return {
                "jobs_scanned": 0,
                "jobs_marked": 0,
                "dirs_removed": 0,
                "files_removed": 0,
                "bytes_removed": 0,
                "paths_skipped": 0,
            }

        now_dt = datetime.now(timezone.utc)
        done_cutoff = (now_dt - timedelta(hours=done_retention)).isoformat()
        failed_cutoff = (now_dt - timedelta(hours=failed_retention)).isoformat()
        now = _utc_now()
        scan_limit = max(1, int(batch_limit))

        jobs_scanned = 0
        jobs_marked = 0
        dirs_removed = 0
        files_removed = 0
        bytes_removed = 0
        paths_skipped = 0

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, product_path, status, outcome_json
                FROM jobs
                WHERE status IN ('done','failed','error','cancelled')
                  AND product_path <> ''
                  AND (
                    (
                      status = 'done'
                      AND ? > 0
                      AND (
                        (finished_at <> '' AND finished_at <= ?)
                        OR (finished_at = '' AND created_at <= ?)
                      )
                    )
                    OR (
                      status IN ('failed','error','cancelled')
                      AND ? > 0
                      AND (
                        (finished_at <> '' AND finished_at <= ?)
                        OR (finished_at = '' AND created_at <= ?)
                      )
                    )
                  )
                ORDER BY finished_at ASC, created_at ASC
                LIMIT ?
                """,
                (
                    done_retention,
                    done_cutoff,
                    done_cutoff,
                    failed_retention,
                    failed_cutoff,
                    failed_cutoff,
                    scan_limit,
                ),
            ).fetchall()

            for row in rows:
                jobs_scanned += 1
                data = row if isinstance(row, dict) else dict(row)
                job_id = str(data.get("id") or "").strip()
                product_path = str(data.get("product_path") or "").strip()
                status = str(data.get("status") or "").strip().lower()
                if not job_id or not product_path:
                    continue

                outcome = self._decode_json(data.get("outcome_json") or "{}")
                if str(outcome.get("__input_pruned_at") or "").strip():
                    continue

                product_dir = Path(product_path)
                if not _is_under_allowed_root(product_dir, safe_roots):
                    paths_skipped += 1
                    continue

                if status == "done":
                    output_dir = product_dir / "output"
                    output_was_pruned = bool(
                        str(outcome.get("__output_pruned_at") or "").strip()
                        and outcome.get("__output_pruned_ok") is not False
                    )
                    if (not output_dir.exists() or not output_dir.is_dir()) and not output_was_pruned:
                        paths_skipped += 1
                        continue

                removed_names: list[str] = []
                removed_files_here = 0
                removed_bytes_here = 0

                for dirname in INPUT_CLEANUP_DIR_NAMES:
                    target = product_dir / dirname
                    if not target.exists() or not target.is_dir():
                        continue
                    if not _is_under_allowed_root(target, (product_dir,)):
                        continue
                    try:
                        for item in target.rglob("*"):
                            if item.is_file():
                                removed_files_here += 1
                                try:
                                    removed_bytes_here += max(0, int(item.stat().st_size))
                                except Exception as exc:
                                    print(f"[input-cleanup][WARN] stat failed path={item}: {exc}")
                    except Exception as exc:
                        print(f"[input-cleanup][WARN] traversal failed path={target}: {exc}")
                    try:
                        shutil.rmtree(target)
                        removed_names.append(dirname)
                    except Exception as exc:
                        print(f"[input-cleanup][WARN] remove failed path={target}: {exc}")
                        continue

                outcome["__input_pruned_at"] = now
                outcome["__input_retention_hours"] = done_retention
                outcome["__input_failed_retention_hours"] = failed_retention
                outcome["__input_pruned_dirs"] = removed_names
                outcome["__input_pruned_files"] = removed_files_here
                outcome["__input_pruned_bytes"] = removed_bytes_here
                outcome["__input_pruned_ok"] = True
                conn.execute(
                    "UPDATE jobs SET outcome_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(outcome, ensure_ascii=False), now, job_id),
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        now,
                        "info",
                        "Input auto-cleanup: "
                        f"status={status} retention_done={done_retention}h retention_failed={failed_retention}h "
                        f"dirs_removed={len(removed_names)} files_removed={removed_files_here} bytes_removed={removed_bytes_here}",
                    ),
                )

                jobs_marked += 1
                dirs_removed += len(removed_names)
                files_removed += removed_files_here
                bytes_removed += removed_bytes_here

        return {
            "jobs_scanned": jobs_scanned,
            "jobs_marked": jobs_marked,
            "dirs_removed": dirs_removed,
            "files_removed": files_removed,
            "bytes_removed": bytes_removed,
            "paths_skipped": paths_skipped,
        }

    def delete_job(self, job_id: str, *, user_id: int | None = None) -> bool:
        return self._queries.delete_job(job_id, user_id=user_id)
