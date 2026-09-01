"""Database connection and schema for job store."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import (
    DB_CONNECT_TIMEOUT_SEC,
    DB_ENGINE,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_READ_TIMEOUT_SEC,
    DB_USER,
    DB_WRITE_TIMEOUT_SEC,
)

_DB_ENGINE = str(DB_ENGINE or "sqlite").strip().lower()
if _DB_ENGINE not in {"sqlite", "mysql"}:
    raise RuntimeError("APP_DB_ENGINE must be either sqlite or mysql.")


def is_mysql_backend() -> bool:
    return _DB_ENGINE == "mysql"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_json(text: Any) -> dict[str, Any]:
    """Decode JSON string safely."""
    try:
        data = json.loads(text or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _ClosedResult:
    def __init__(self, lastrowid: Any = None, rowcount: int = -1) -> None:
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


class _ResultCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.lastrowid = getattr(cursor, "lastrowid", None)
        self.rowcount = getattr(cursor, "rowcount", -1)

    def fetchone(self) -> Any:
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        try:
            self._cursor.close()
        except Exception as exc:
            print(f"[db][WARN] cursor close after fetchone failed: {exc}")
        self._cursor = None
        return row

    def fetchall(self) -> list[Any]:
        if self._cursor is None:
            return []
        rows = self._cursor.fetchall()
        try:
            self._cursor.close()
        except Exception as exc:
            print(f"[db][WARN] cursor close after fetchall failed: {exc}")
        self._cursor = None
        return list(rows)


class _DbConnection:
    def __init__(self, raw_conn: Any, backend: str) -> None:
        self._raw_conn = raw_conn
        self.backend = backend

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        params = tuple(params or ())
        if self.backend == "mysql":
            cursor = self._raw_conn.cursor()
            mysql_sql = sql.replace("?", "%s")
            cursor.execute(mysql_sql, params)
            if cursor.description is None:
                result = _ClosedResult(lastrowid=getattr(cursor, "lastrowid", None), rowcount=getattr(cursor, "rowcount", -1))
                cursor.close()
                return result
            return _ResultCursor(cursor)

        cursor = self._raw_conn.execute(sql, params)
        if cursor.description is None:
            return _ClosedResult(lastrowid=getattr(cursor, "lastrowid", None), rowcount=getattr(cursor, "rowcount", -1))
        return _ResultCursor(cursor)

    def commit(self) -> None:
        self._raw_conn.commit()

    def rollback(self) -> None:
        self._raw_conn.rollback()

    def close(self) -> None:
        self._raw_conn.close()

    def __enter__(self) -> "_DbConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def _connect_sqlite(db_path: Path) -> _DbConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return _DbConnection(conn, backend="sqlite")


def _connect_mysql() -> _DbConnection:
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except Exception as exc:
        raise RuntimeError("MySQL backend requires `pymysql` package installed") from exc

    host = str(DB_HOST).strip()
    port = int(DB_PORT)
    user = str(DB_USER).strip()
    password = str(DB_PASSWORD)
    database = str(DB_NAME).strip()
    if not user or not database:
        raise RuntimeError("MySQL backend requires APP_DB_USER and APP_DB_NAME")

    raw = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=int(DB_CONNECT_TIMEOUT_SEC),
        read_timeout=int(DB_READ_TIMEOUT_SEC),
        write_timeout=int(DB_WRITE_TIMEOUT_SEC),
    )
    with raw.cursor() as cur:
        cur.execute("SET SESSION innodb_lock_wait_timeout = 30")
    return _DbConnection(raw, backend="mysql")


def _connect(db_path: Path) -> _DbConnection:
    """Create a database connection for the configured backend."""
    return _connect_mysql() if is_mysql_backend() else _connect_sqlite(Path(db_path))


def _is_duplicate_column_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "duplicate column" in text or "1060" in text


def _is_duplicate_index_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "duplicate key name" in text or "already exists" in text or "1061" in text


def _init_schema_sqlite(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL DEFAULT 0,
                product_path TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                scan_summary_json TEXT NOT NULL DEFAULT '{}',
                outcome_json TEXT NOT NULL DEFAULT '{}',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                expired_at TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                run_token TEXT NOT NULL DEFAULT '',
                heartbeat_at TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                failure_code TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_job_id_id ON job_events(job_id, id)")

        for col, dtype in [
            ("user_id", "INTEGER NOT NULL DEFAULT 0"),
            ("total_input_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("total_output_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("total_outputs", "INTEGER NOT NULL DEFAULT 0"),
            ("expired_at", "TEXT NOT NULL DEFAULT ''"),
            ("worker_id", "TEXT NOT NULL DEFAULT ''"),
            ("run_token", "TEXT NOT NULL DEFAULT ''"),
            ("heartbeat_at", "TEXT NOT NULL DEFAULT ''"),
            ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("failure_code", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    print(f"[db][WARN] sqlite schema column migration failed column={col}: {exc}")
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_status_created ON jobs(user_id, status, created_at)")
        except Exception as exc:
            print(f"[db][WARN] sqlite user/status index migration failed: {exc}")
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_lease ON jobs(status, lease_expires_at)")
        except Exception as exc:
            print(f"[db][WARN] sqlite lease index migration failed: {exc}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS abroll_jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL DEFAULT 0,
                settings_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS abroll_job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES abroll_jobs(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abroll_status_created ON abroll_jobs(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abroll_events_job_id_id ON abroll_job_events(job_id, id)")


def _init_schema_mysql(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id VARCHAR(64) PRIMARY KEY,
                user_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
                product_path TEXT NOT NULL,
                mode VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL,
                settings_json LONGTEXT NOT NULL,
                scan_summary_json LONGTEXT NOT NULL,
                outcome_json LONGTEXT NOT NULL,
                cancel_requested TINYINT(1) NOT NULL DEFAULT 0,
                error TEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                started_at VARCHAR(64) NOT NULL,
                finished_at VARCHAR(64) NOT NULL,
                expired_at VARCHAR(64) NOT NULL DEFAULT '',
                total_input_bytes BIGINT NOT NULL DEFAULT 0,
                total_output_bytes BIGINT NOT NULL DEFAULT 0,
                total_outputs BIGINT NOT NULL DEFAULT 0,
                worker_id VARCHAR(128) NOT NULL DEFAULT '',
                run_token VARCHAR(64) NOT NULL DEFAULT '',
                heartbeat_at VARCHAR(64) NOT NULL DEFAULT '',
                lease_expires_at VARCHAR(64) NOT NULL DEFAULT '',
                attempt_count BIGINT NOT NULL DEFAULT 0,
                failure_code VARCHAR(64) NOT NULL DEFAULT '',
                INDEX idx_jobs_status_created (status, created_at),
                INDEX idx_jobs_user_status_created (user_id, status, created_at),
                INDEX idx_jobs_status_lease (status, lease_expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                job_id VARCHAR(64) NOT NULL,
                ts VARCHAR(64) NOT NULL,
                level VARCHAR(32) NOT NULL,
                message LONGTEXT NOT NULL,
                INDEX idx_events_job_id_id (job_id, id),
                CONSTRAINT fk_job_events_job FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        for col, dtype in [
            ("user_id", "BIGINT UNSIGNED NOT NULL DEFAULT 0"),
            ("total_input_bytes", "BIGINT NOT NULL DEFAULT 0"),
            ("total_output_bytes", "BIGINT NOT NULL DEFAULT 0"),
            ("total_outputs", "BIGINT NOT NULL DEFAULT 0"),
            ("expired_at", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("worker_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("run_token", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("heartbeat_at", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("lease_expires_at", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("attempt_count", "BIGINT NOT NULL DEFAULT 0"),
            ("failure_code", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            except Exception as exc:
                if not _is_duplicate_column_error(exc):
                    raise
        for index_sql in [
            "CREATE INDEX idx_jobs_status_created ON jobs(status, created_at)",
            "CREATE INDEX idx_jobs_user_status_created ON jobs(user_id, status, created_at)",
            "CREATE INDEX idx_events_job_id_id ON job_events(job_id, id)",
            "CREATE INDEX idx_jobs_status_lease ON jobs(status, lease_expires_at)",
        ]:
            try:
                conn.execute(index_sql)
            except Exception as exc:
                if not _is_duplicate_index_error(exc):
                    raise

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS abroll_jobs (
                id VARCHAR(64) PRIMARY KEY,
                user_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
                settings_json LONGTEXT NOT NULL,
                status VARCHAR(32) NOT NULL,
                error TEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                started_at VARCHAR(64) NOT NULL,
                finished_at VARCHAR(64) NOT NULL,
                INDEX idx_abroll_status_created (status, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS abroll_job_events (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                job_id VARCHAR(64) NOT NULL,
                ts VARCHAR(64) NOT NULL,
                level VARCHAR(32) NOT NULL,
                message LONGTEXT NOT NULL,
                INDEX idx_abroll_events_job_id_id (job_id, id),
                CONSTRAINT fk_abroll_job_events_job FOREIGN KEY (job_id) REFERENCES abroll_jobs(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )


def init_schema(db_path: Path) -> None:
    """Initialize the database schema for SQLite or MySQL."""
    if is_mysql_backend():
        _init_schema_mysql(db_path)
    else:
        _init_schema_sqlite(db_path)


def row_to_job(row: Any | None) -> dict[str, Any] | None:
    """Convert a database row to a job dictionary."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "user_id": int(row.get("user_id") or 0) if isinstance(row, dict) else int(row["user_id"] or 0),
        "product_path": row["product_path"],
        "mode": row["mode"],
        "status": row["status"],
        "settings": _decode_json(row["settings_json"]),
        "scan_summary": _decode_json(row["scan_summary_json"]),
        "outcome": _decode_json(row["outcome_json"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "total_input_bytes": row["total_input_bytes"],
        "total_output_bytes": row["total_output_bytes"],
        "total_outputs": row["total_outputs"],
        "expired_at": row["expired_at"],
        "worker_id": row.get("worker_id", "") if isinstance(row, dict) else row["worker_id"],
        "run_token": row.get("run_token", "") if isinstance(row, dict) else row["run_token"],
        "heartbeat_at": row.get("heartbeat_at", "") if isinstance(row, dict) else row["heartbeat_at"],
        "lease_expires_at": row.get("lease_expires_at", "") if isinstance(row, dict) else row["lease_expires_at"],
        "attempt_count": int(row.get("attempt_count", 0) or 0) if isinstance(row, dict) else int(row["attempt_count"] or 0),
        "failure_code": row.get("failure_code", "") if isinstance(row, dict) else row["failure_code"],
    }
