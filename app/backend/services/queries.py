"""Job CRUD operations for job store."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import JOB_QUEUE_SOFT_LIMIT
from .db import _connect, _utc_now, init_schema, is_mysql_backend, row_to_job
from .event_level import storage_event_level


_SCHEMA_INIT_LOCK = threading.Lock()
_SCHEMA_INITIALIZED_KEYS: set[str] = set()
_SCHEMA_INIT_RETRY_DELAYS = (0.1, 0.25, 0.5)
_TRANSIENT_MYSQL_LOCK_ERRORS = {1205, 1213}


def _schema_key(db_path: Path) -> str:
    return str(Path(db_path).resolve())


def _mysql_error_code(exc: Exception) -> int | None:
    args = getattr(exc, "args", ())
    if not args:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


def _ensure_schema_initialized(db_path: Path) -> None:
    """Run schema DDL once per database path in this process.

    Route handlers create lightweight ``JobStore`` wrappers. Re-running the
    MySQL ``ALTER TABLE`` compatibility checks for every request can deadlock
    when several API calls arrive together while a worker updates ``jobs``.
    """
    key = _schema_key(db_path)
    if key in _SCHEMA_INITIALIZED_KEYS:
        return

    with _SCHEMA_INIT_LOCK:
        if key in _SCHEMA_INITIALIZED_KEYS:
            return

        for attempt in range(len(_SCHEMA_INIT_RETRY_DELAYS) + 1):
            try:
                init_schema(db_path)
                _SCHEMA_INITIALIZED_KEYS.add(key)
                return
            except Exception as exc:
                code = _mysql_error_code(exc)
                if code not in _TRANSIENT_MYSQL_LOCK_ERRORS or attempt >= len(_SCHEMA_INIT_RETRY_DELAYS):
                    raise
                time.sleep(_SCHEMA_INIT_RETRY_DELAYS[attempt])


def _database_utc_now(conn: Any) -> datetime:
    """Use the MySQL clock for leases so host clock skew cannot revive a run."""
    if not is_mysql_backend():
        return datetime.now(timezone.utc)
    row = conn.execute("SELECT UTC_TIMESTAMP(6) AS now_utc").fetchone()
    value = row["now_utc"] if row else None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class JobQueries:
    """CRUD operations for jobs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if not is_mysql_backend():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        _ensure_schema_initialized(self.db_path)

    def _connect(self) -> Any:
        return _connect(self.db_path)

    @staticmethod
    def _normalized_user_id(user_id: int | None) -> int | None:
        if user_id is None:
            return None
        try:
            value = int(user_id)
        except Exception:
            return None
        return value if value >= 0 else None

    def _append_user_filter(
        self,
        where_clauses: list[str],
        params: list[Any],
        user_id: int | None,
    ) -> None:
        normalized = self._normalized_user_id(user_id)
        if normalized is None:
            return
        where_clauses.append("user_id = ?")
        params.append(normalized)

    def create_job(
        self,
        *,
        product_path: str,
        mode: str,
        settings: dict[str, Any] | None = None,
        status: str = "queued",
        user_id: int = 0,
    ) -> dict[str, Any]:
        """Create a new job."""
        now = _utc_now()
        job_id = uuid.uuid4().hex
        owner_id = self._normalized_user_id(user_id)
        if owner_id is None:
            owner_id = 0
        initial_status = str(status or "queued").strip() or "queued"
        settings_json = json.dumps(settings or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, user_id, product_path, mode, status,
                    settings_json, scan_summary_json, outcome_json, cancel_requested, error,
                    created_at, updated_at, started_at, finished_at, expired_at,
                    total_input_bytes, total_output_bytes, total_outputs
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', '{}', 0, '', ?, ?, '', '', '', 0, 0, 0)
                """,
                (job_id, owner_id, product_path, mode, initial_status, settings_json, now, now),
            )
        job = self.get_job(job_id, user_id=owner_id)
        if not job:
            raise RuntimeError("Failed to create job")
        return job

    def get_job(self, job_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
        """Get a job by ID."""
        params: list[Any] = [job_id]
        where = ["id = ?"]
        self._append_user_filter(where, params, user_id)
        sql = f"SELECT * FROM jobs WHERE {' AND '.join(where)}"
        with self._connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return row_to_job(row)

    def list_jobs(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List jobs with optional status filter."""
        params: list[Any] = []
        where: list[str] = []
        self._append_user_filter(where, params, user_id)
        if status:
            where.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        sql = f"SELECT * FROM jobs {where_sql} ORDER BY created_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [job for job in (row_to_job(row) for row in rows) if job is not None]

    def add_event(
        self,
        job_id: str,
        message: str,
        level: str = "info",
        *,
        run_token: str | None = None,
    ) -> int:
        """Add an event; a supplied token rejects output from stale workers."""
        ts = _utc_now()
        normalized_message = str(message)
        normalized_level = storage_event_level(level, normalized_message)
        with self._lock, self._connect() as conn:
            if run_token:
                owner = conn.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ? AND status = 'running' AND run_token = ?",
                    (ts, job_id, run_token),
                )
                if owner.rowcount <= 0:
                    return 0
            cur = conn.execute(
                "INSERT INTO job_events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (job_id, ts, normalized_level, normalized_message),
            )
            if not run_token:
                conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (ts, job_id))
            return int(cur.lastrowid)

    def get_events(self, job_id: str, *, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Get events for a job."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, ts, level, message
                FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (job_id, after_id, limit),
            ).fetchall()
        return [
            {"id": row["id"], "job_id": row["job_id"], "ts": row["ts"], "level": row["level"], "message": row["message"]}
            for row in rows
        ]

    def request_cancel(self, job_id: str, *, user_id: int | None = None) -> bool:
        """Request cancellation of a job."""
        now = _utc_now()
        where = ["id = ?", "status IN ('queued', 'running')"]
        params: list[Any] = [now, job_id]
        self._append_user_filter(where, params, user_id)
        sql = f"""
                UPDATE jobs
                SET cancel_requested = 1, updated_at = ?
                WHERE {' AND '.join(where)}
                """
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount > 0

    def is_cancel_requested(self, job_id: str, *, run_token: str | None = None) -> bool:
        """Check cancellation; a missing fenced running row is fail-closed."""
        with self._connect() as conn:
            if run_token:
                row = conn.execute(
                    "SELECT cancel_requested FROM jobs WHERE id = ? AND status = 'running' AND run_token = ?",
                    (job_id, run_token),
                ).fetchone()
                if not row:
                    return True
            else:
                row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return bool(row and row["cancel_requested"])

    def claim_next_queued_job(
        self,
        *,
        worker_id: str = "legacy-worker",
        run_token: str | None = None,
        lease_ttl_sec: int = 90,
        modes: tuple[str, ...] | None = None,
        exclude_modes: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one queued job and fence it with a unique run token."""
        token = str(run_token or "").strip() or uuid.uuid4().hex
        owner = str(worker_id or "legacy-worker").strip()[:128] or "legacy-worker"
        where = ["status = 'queued'"]
        select_params: list[Any] = []
        normalized_modes = tuple(str(item).strip().lower() for item in (modes or ()) if str(item).strip())
        normalized_excluded = tuple(
            str(item).strip().lower() for item in (exclude_modes or ()) if str(item).strip()
        )
        if normalized_modes:
            where.append(f"LOWER(mode) IN ({','.join('?' for _ in normalized_modes)})")
            select_params.extend(normalized_modes)
        if normalized_excluded:
            where.append(f"LOWER(mode) NOT IN ({','.join('?' for _ in normalized_excluded)})")
            select_params.extend(normalized_excluded)
        select_sql = f"SELECT id FROM jobs WHERE {' AND '.join(where)} ORDER BY created_at ASC LIMIT 1"
        with self._lock, self._connect() as conn:
            if is_mysql_backend():
                conn.execute("START TRANSACTION")
            now_dt = _database_utc_now(conn)
            now = now_dt.isoformat()
            lease_expires_at = (
                now_dt + timedelta(seconds=max(30, int(lease_ttl_sec)))
            ).isoformat()
            if is_mysql_backend():
                row = conn.execute(f"{select_sql} FOR UPDATE", tuple(select_params)).fetchone()
            else:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(select_sql, tuple(select_params)).fetchone()
            if not row:
                conn.commit()
                return None
            job_id = row["id"]
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?, updated_at = ?,
                    worker_id = ?, run_token = ?, heartbeat_at = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1, failure_code = ''
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, owner, token, now, lease_expires_at, job_id),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            conn.commit()
        return self.get_job(job_id)

    def renew_job_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        run_token: str,
        lease_ttl_sec: int = 90,
    ) -> bool:
        """Extend a running job lease only when owner and fencing token still match."""
        with self._lock, self._connect() as conn:
            now_dt = _database_utc_now(conn)
            now = now_dt.isoformat()
            lease_expires_at = (
                now_dt + timedelta(seconds=max(30, int(lease_ttl_sec)))
            ).isoformat()
            cur = conn.execute(
                """
                UPDATE jobs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ? AND run_token = ?
                  AND lease_expires_at > ?
                """,
                (now, lease_expires_at, now, job_id, worker_id, run_token, now),
            )
            return cur.rowcount > 0

    def update_job_progress_heartbeat(self, job_id: str) -> bool:
        """Update last_heartbeat_at to signal job processing is making progress.

        Called by _ProgressHeartbeatThread in worker.py when the main processing
        thread has pinged recently. The watchdog checks last_heartbeat_at to
        distinguish 'worker dead' (lease thread stopped) from 'processing hung'
        (lease thread alive but main thread stuck).

        TIMEZONE NOTE: We use MySQL's NOW() directly instead of UTC.
        Reason: MySQL session timezone is SYSTEM (ICT on this host). If we
        write a UTC value, MySQL converts to ICT for storage. The watchdog
        uses NOW() (session time) for comparison. Mixing UTC write with
        session-time read causes diff to be off by 7 hours.

        Using NOW() for both write and read guarantees consistency. The
        trade-off: progress heartbeat no longer uses UTC clock (lease
        heartbeat still does, where cross-machine consistency matters).

        On SQLite (non-MySQL), we use Python's local time which is also
        "session time" and matches what the watchdog sees via datetime().
        """
        if not str(job_id or "").strip():
            return False
        with self._lock, self._connect() as conn:
            if is_mysql_backend():
                # Use MySQL's NOW() directly for write-read consistency
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET last_heartbeat_at = NOW(), updated_at = NOW()
                    WHERE id = ? AND status = 'running'
                    """,
                    (job_id,),
                )
            else:
                # SQLite: use local time
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET last_heartbeat_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, now, job_id),
                )
            return cur.rowcount > 0

    def release_claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        run_token: str,
        reason: str = "worker_draining",
    ) -> bool:
        """Return an unstarted claim to queued using owner+token compare-and-set."""
        with self._lock, self._connect() as conn:
            now = _database_utc_now(conn).isoformat()
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', updated_at = ?, started_at = '',
                    worker_id = '', run_token = '', heartbeat_at = '', lease_expires_at = '',
                    failure_code = '',
                    attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END
                WHERE id = ? AND status = 'running' AND worker_id = ? AND run_token = ?
                """,
                (now, job_id, worker_id, run_token),
            )
            if cur.rowcount <= 0:
                return False
            conn.execute(
                "INSERT INTO job_events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (job_id, now, "info", f"claim_released: {str(reason or 'worker_draining')[:120]}"),
            )
            return True

    def retry_running_job(self, job_id: str, *, run_token: str) -> bool:
        """Atomically transition the current fenced attempt back to queued."""
        with self._lock, self._connect() as conn:
            now = _database_utc_now(conn).isoformat()
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', cancel_requested = 0, error = '',
                    outcome_json = '{}', scan_summary_json = '{}', updated_at = ?,
                    started_at = '', finished_at = '', worker_id = '', run_token = '',
                    heartbeat_at = '', lease_expires_at = '', failure_code = ''
                WHERE id = ? AND status = 'running' AND run_token = ?
                """,
                (now, job_id, run_token),
            )
            return cur.rowcount > 0

    def has_queued_jobs(self) -> bool:
        """Fast check whether there is any queued job."""
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            return bool(row)

    def get_queue_load(self, *, user_id: int | None = None) -> dict[str, int]:
        """Return queue load counters (queued/running/total)."""
        where: list[str] = ["status IN ('queued', 'running')"]
        params: list[Any] = []
        self._append_user_filter(where, params, user_id)
        sql = f"""
            SELECT status, COUNT(*) AS count_value
            FROM jobs
            WHERE {' AND '.join(where)}
            GROUP BY status
        """
        queued = 0
        running = 0
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        for row in rows:
            status = str(row.get("status") if isinstance(row, dict) else row["status"]).strip().lower()
            count_value = int(row.get("count_value") if isinstance(row, dict) else row["count_value"])
            if status == "queued":
                queued = count_value
            elif status == "running":
                running = count_value
        return {"queued": queued, "running": running, "total": queued + running}

    def get_job_metrics_rows(self, *, limit: int = 2000, user_id: int | None = None) -> list[dict[str, Any]]:
        """Return recent jobs for metrics calculations."""
        params: list[Any] = []
        where: list[str] = []
        self._append_user_filter(where, params, user_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, int(limit)))
        sql = f"""
            SELECT id, status, created_at, started_at, finished_at, updated_at, error
            FROM jobs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            data = row if isinstance(row, dict) else dict(row)
            output.append(
                {
                    "id": data.get("id", ""),
                    "status": data.get("status", ""),
                    "created_at": data.get("created_at", ""),
                    "started_at": data.get("started_at", ""),
                    "finished_at": data.get("finished_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "error": data.get("error", ""),
                }
            )
        return output

    def get_recent_incident_events(
        self,
        *,
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent warn/error job events for incident feeds."""
        params: list[Any] = ["warn", "error"]
        where: list[str] = ["e.level IN (?, ?)"]
        if self._normalized_user_id(user_id) is not None:
            where.append("j.user_id = ?")
            params.append(self._normalized_user_id(user_id))
        params.append(max(1, int(limit)))
        sql = f"""
            SELECT e.id, e.job_id, e.ts, e.level, e.message
            FROM job_events e
            INNER JOIN jobs j ON j.id = e.job_id
            WHERE {' AND '.join(where)}
            ORDER BY e.id DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            data = row if isinstance(row, dict) else dict(row)
            output.append(
                {
                    "id": int(data.get("id") or 0),
                    "job_id": str(data.get("job_id") or ""),
                    "ts": str(data.get("ts") or ""),
                    "level": str(data.get("level") or "info"),
                    "message": str(data.get("message") or ""),
                }
            )
        return output

    def update_scan_summary(
        self,
        job_id: str,
        scan_summary: dict[str, Any],
        *,
        run_token: str | None = None,
    ) -> bool:
        """Update scan summary for a job."""
        now = _utc_now()
        payload = json.dumps(scan_summary or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            if run_token:
                cur = conn.execute(
                    "UPDATE jobs SET scan_summary_json = ?, updated_at = ? WHERE id = ? AND status = 'running' AND run_token = ?",
                    (payload, now, job_id, run_token),
                )
            else:
                cur = conn.execute(
                    "UPDATE jobs SET scan_summary_json = ?, updated_at = ? WHERE id = ?",
                    (payload, now, job_id),
                )
            return cur.rowcount > 0

    def update_job_input(
        self,
        job_id: str,
        *,
        product_path: str | None = None,
        settings: dict[str, Any] | None = None,
        user_id: int | None = None,
        run_token: str | None = None,
    ) -> bool:
        """Update job input parameters."""
        now = _utc_now()
        updates: list[str] = []
        params: list[Any] = []
        if product_path is not None:
            updates.append("product_path = ?")
            params.append(product_path)
        if settings is not None:
            updates.append("settings_json = ?")
            params.append(json.dumps(settings or {}, ensure_ascii=False))
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(now)
        where = ["id = ?"]
        params.append(job_id)
        self._append_user_filter(where, params, user_id)
        if run_token:
            where.extend(["status = 'running'", "run_token = ?"])
            params.append(run_token)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(updates)} WHERE {' AND '.join(where)}",
                tuple(params),
            )
            return cur.rowcount > 0

    def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        clear_error: bool = False,
        reset_run_state: bool = False,
        user_id: int | None = None,
    ) -> bool:
        """Set job status."""
        now = _utc_now()
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if clear_error:
            updates.append("error = ''")
        if reset_run_state:
            updates.extend([
                "cancel_requested = 0",
                "outcome_json = '{}'",
                "scan_summary_json = '{}'",
                "started_at = ''",
                "finished_at = ''",
                "worker_id = ''",
                "run_token = ''",
                "heartbeat_at = ''",
                "lease_expires_at = ''",
                "failure_code = ''",
            ])
        where = ["id = ?"]
        params.append(job_id)
        self._append_user_filter(where, params, user_id)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(updates)} WHERE {' AND '.join(where)}",
                tuple(params),
            )
            return cur.rowcount > 0

    def queue_job(self, job_id: str, *, user_id: int | None = None) -> bool:
        """Queue a job for processing."""
        if JOB_QUEUE_SOFT_LIMIT > 0:
            load = self.get_queue_load(user_id=user_id)
            if int(load.get("total") or 0) >= int(JOB_QUEUE_SOFT_LIMIT):
                return False
        now = _utc_now()
        where = ["id = ?", "status IN ('created', 'failed', 'cancelled')"]
        params: list[Any] = [now, job_id]
        self._append_user_filter(where, params, user_id)
        sql = f"""
                UPDATE jobs
                SET status = 'queued',
                    cancel_requested = 0,
                    error = '',
                    outcome_json = '{{}}',
                    scan_summary_json = '{{}}',
                    updated_at = ?,
                    started_at = '',
                    finished_at = '',
                    worker_id = '',
                    run_token = '',
                    heartbeat_at = '',
                    lease_expires_at = '',
                    failure_code = ''
                WHERE {' AND '.join(where)}
                """
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount > 0

    def list_completed_jobs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        mode: str | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List completed jobs."""
        params: list[Any] = []
        where = ["status IN ('done','failed','error','cancelled')"]
        self._append_user_filter(where, params, user_id)
        if mode:
            where.append("mode = ?")
            params.append(mode)
        params.extend([limit, offset])
        sql = f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY finished_at DESC LIMIT ? OFFSET ?"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [job for job in (row_to_job(row) for row in rows) if job is not None]

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
        """Mark a job finished; a supplied token fences stale workers."""
        now = _utc_now()
        payload = json.dumps(outcome or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            where = "id = ?"
            params: list[Any] = [
                status,
                payload,
                error,
                now,
                now,
                total_input_bytes,
                total_output_bytes,
                total_outputs,
                str(failure_code or "")[:64],
                job_id,
            ]
            if run_token:
                where += " AND status = 'running' AND run_token = ?"
                params.append(run_token)
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET status = ?, outcome_json = ?, error = ?, updated_at = ?, finished_at = ?,
                    total_input_bytes = ?, total_output_bytes = ?, total_outputs = ?,
                    failure_code = ?, worker_id = '', run_token = '',
                    heartbeat_at = '', lease_expires_at = ''
                WHERE {where}
                """,
                tuple(params),
            )
            return cur.rowcount > 0

    def cancel_queued_job(self, job_id: str, *, user_id: int | None = None) -> bool:
        """Cancel a queued job."""
        now = _utc_now()
        where = ["id = ?", "status = 'queued'"]
        params: list[Any] = [now, now, job_id]
        self._append_user_filter(where, params, user_id)
        sql = f"""
                UPDATE jobs
                SET status = 'cancelled', cancel_requested = 1, updated_at = ?, finished_at = ?, error = 'Cancelled before start'
                WHERE {' AND '.join(where)}
                """
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount > 0

    def requeue_job(
        self,
        job_id: str,
        *,
        user_id: int | None = None,
        run_token: str | None = None,
    ) -> bool:
        """Requeue a failed or cancelled job."""
        if run_token:
            # Worker retries transition directly from running via
            # retry_running_job(). Terminal rows are operator-owned only.
            return False
        if JOB_QUEUE_SOFT_LIMIT > 0:
            load = self.get_queue_load(user_id=user_id)
            if int(load.get("total") or 0) >= int(JOB_QUEUE_SOFT_LIMIT):
                return False
        now = _utc_now()
        where = ["id = ?", "status IN ('failed', 'cancelled')"]
        params: list[Any] = [now, job_id]
        self._append_user_filter(where, params, user_id)
        sql = f"""
                UPDATE jobs
                SET status = 'queued',
                    cancel_requested = 0,
                    error = '',
                    outcome_json = '{{}}',
                    updated_at = ?,
                    started_at = '',
                    finished_at = '',
                    worker_id = '',
                    run_token = '',
                    heartbeat_at = '',
                    lease_expires_at = '',
                    failure_code = ''
                WHERE {' AND '.join(where)}
                """
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount > 0

    def delete_job(self, job_id: str, *, user_id: int | None = None) -> bool:
        """Delete a finished job and its events from the database.

        Only jobs with terminal status (done, failed, error, cancelled) can be deleted.
        Returns True if the job was deleted.
        """
        params: list[Any] = [job_id]
        where = ["id = ?", "status IN ('done','failed','error','cancelled')"]
        self._append_user_filter(where, params, user_id)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
            cur = conn.execute(
                f"DELETE FROM jobs WHERE {' AND '.join(where)}",
                tuple(params),
            )
            return cur.rowcount > 0

    def recover_expired_leases(self) -> list[str]:
        """Fail expired leases as ``worker_lost`` without unsafe automatic retry."""
        recovered: list[str] = []
        with self._lock, self._connect() as conn:
            now = _database_utc_now(conn).isoformat()
            rows = conn.execute(
                """
                SELECT id, run_token, lease_expires_at
                FROM jobs
                WHERE status = 'running' AND lease_expires_at <> '' AND lease_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                job_id = row["id"]
                run_token = row["run_token"]
                lease_expires_at = row["lease_expires_at"]
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        error = 'Worker lease expired before completion',
                        failure_code = 'worker_lost',
                        updated_at = ?,
                        finished_at = ?,
                        run_token = '',
                        heartbeat_at = '',
                        lease_expires_at = ''
                    WHERE id = ? AND status = 'running' AND run_token = ? AND lease_expires_at = ?
                    """,
                    (now, now, job_id, run_token, lease_expires_at),
                )
                if cur.rowcount > 0:
                    conn.execute(
                        "INSERT INTO job_events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                        (job_id, now, "error", "worker_lost: job lease expired; manual retry required"),
                    )
                    recovered.append(job_id)
        return recovered

    def recover_stale_jobs(self, *, stale_minutes: int = 30) -> list[str]:
        """Compatibility alias; recovery is lease-based and never requeues."""
        _ = stale_minutes
        return self.recover_expired_leases()
