"""Library metadata, folders, notifications, devices, and guest recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import BROWSER_IDENTITY_SECRET
from .db import _connect, is_mysql_backend


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:48] for item in value if str(item).strip()][:20]
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return _json_list(parsed)


class WorkspaceService:
    """Persist UI/UX state independently from processing job payloads."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _connect(self):
        return _connect(self.db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            if is_mysql_backend():
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS job_library_meta (
                    job_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL,
                    display_name VARCHAR(255) NOT NULL DEFAULT '', folder_id VARCHAR(64) NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL, pinned TINYINT NOT NULL DEFAULT 0,
                    favorite TINYINT NOT NULL DEFAULT 0, retention_until VARCHAR(64) NOT NULL DEFAULT '',
                    created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                    INDEX idx_library_user_updated (user_id, updated_at),
                    INDEX idx_library_user_folder (user_id, folder_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS job_folders (
                    id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, name VARCHAR(120) NOT NULL,
                    color VARCHAR(32) NOT NULL DEFAULT 'violet', created_at VARCHAR(64) NOT NULL,
                    updated_at VARCHAR(64) NOT NULL, UNIQUE KEY uq_folder_user_name (user_id, name),
                    INDEX idx_folder_user (user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS notifications (
                    id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, event_key VARCHAR(190) NOT NULL,
                    kind VARCHAR(48) NOT NULL, severity VARCHAR(24) NOT NULL DEFAULT 'info',
                    title VARCHAR(255) NOT NULL, body TEXT NOT NULL, job_id VARCHAR(64) NOT NULL DEFAULT '',
                    action_url VARCHAR(512) NOT NULL DEFAULT '', read_at VARCHAR(64) NOT NULL DEFAULT '',
                    created_at VARCHAR(64) NOT NULL, UNIQUE KEY uq_notification_event (user_id, event_key),
                    INDEX idx_notification_user_created (user_id, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS notification_preferences (
                    user_id BIGINT PRIMARY KEY, browser_enabled TINYINT NOT NULL DEFAULT 1,
                    sound_enabled TINYINT NOT NULL DEFAULT 1, line_enabled TINYINT NOT NULL DEFAULT 0,
                    email_enabled TINYINT NOT NULL DEFAULT 0, line_target VARCHAR(255) NOT NULL DEFAULT '',
                    email_address VARCHAR(255) NOT NULL DEFAULT '', updated_at VARCHAR(64) NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS guest_recovery_codes (
                    id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, code_hash CHAR(64) NOT NULL UNIQUE,
                    expires_at VARCHAR(64) NOT NULL, used_at VARCHAR(64) NOT NULL DEFAULT '',
                    created_at VARCHAR(64) NOT NULL, INDEX idx_recovery_user (user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
            else:
                statements = [
                    """CREATE TABLE IF NOT EXISTS job_library_meta (
                    job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, display_name TEXT NOT NULL DEFAULT '',
                    folder_id TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
                    pinned INTEGER NOT NULL DEFAULT 0, favorite INTEGER NOT NULL DEFAULT 0,
                    retention_until TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS job_folders (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT 'violet',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id, name))""",
                    """CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL, body TEXT NOT NULL,
                    job_id TEXT NOT NULL DEFAULT '', action_url TEXT NOT NULL DEFAULT '', read_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, UNIQUE(user_id, event_key))""",
                    """CREATE TABLE IF NOT EXISTS notification_preferences (
                    user_id INTEGER PRIMARY KEY, browser_enabled INTEGER NOT NULL DEFAULT 1,
                    sound_enabled INTEGER NOT NULL DEFAULT 1, line_enabled INTEGER NOT NULL DEFAULT 0,
                    email_enabled INTEGER NOT NULL DEFAULT 0, line_target TEXT NOT NULL DEFAULT '',
                    email_address TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS guest_recovery_codes (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, code_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL, used_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
                ]
                for sql in statements:
                    conn.execute(sql)
                indexes = [
                    "CREATE INDEX IF NOT EXISTS idx_library_user_updated ON job_library_meta(user_id, updated_at)",
                    "CREATE INDEX IF NOT EXISTS idx_library_user_folder ON job_library_meta(user_id, folder_id)",
                    "CREATE INDEX IF NOT EXISTS idx_folder_user ON job_folders(user_id)",
                    "CREATE INDEX IF NOT EXISTS idx_notification_user_created ON notifications(user_id, created_at)",
                    "CREATE INDEX IF NOT EXISTS idx_recovery_user ON guest_recovery_codes(user_id)",
                ]
                for sql in indexes:
                    conn.execute(sql)

    def metadata_map(self, user_id: int, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not job_ids:
            return {}
        marks = ",".join("?" for _ in job_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM job_library_meta WHERE user_id = ? AND job_id IN ({marks})",
                (int(user_id), *job_ids),
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["tags"] = _json_list(item.pop("tags_json", "[]"))
            item["pinned"] = bool(item.get("pinned"))
            item["favorite"] = bool(item.get("favorite"))
            output[str(item["job_id"])] = item
        return output

    def update_meta(self, user_id: int, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            old_row = conn.execute(
                "SELECT * FROM job_library_meta WHERE user_id = ? AND job_id = ?",
                (int(user_id), job_id),
            ).fetchone()
            old = dict(old_row) if old_row else {}
            values = {
                "display_name": str(payload.get("display_name", old.get("display_name", ""))).strip()[:255],
                "folder_id": str(payload.get("folder_id", old.get("folder_id", ""))).strip()[:64],
                "tags_json": json.dumps(_json_list(payload.get("tags", old.get("tags_json", "[]"))), ensure_ascii=False),
                "pinned": int(bool(payload.get("pinned", old.get("pinned", 0)))),
                "favorite": int(bool(payload.get("favorite", old.get("favorite", 0)))),
                "retention_until": str(payload.get("retention_until", old.get("retention_until", ""))).strip()[:64],
            }
            if old:
                conn.execute(
                    """UPDATE job_library_meta SET display_name=?, folder_id=?, tags_json=?, pinned=?,
                    favorite=?, retention_until=?, updated_at=? WHERE user_id=? AND job_id=?""",
                    (*values.values(), now, int(user_id), job_id),
                )
            else:
                conn.execute(
                    """INSERT INTO job_library_meta(job_id,user_id,display_name,folder_id,tags_json,pinned,
                    favorite,retention_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, int(user_id), *values.values(), now, now),
                )
        return self.metadata_map(user_id, [job_id]).get(job_id, {})

    def list_folders(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT f.*, COUNT(m.job_id) AS job_count FROM job_folders f
                LEFT JOIN job_library_meta m ON m.folder_id=f.id AND m.user_id=f.user_id
                WHERE f.user_id=? GROUP BY f.id ORDER BY f.name""",
                (int(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_folder(self, user_id: int, name: str, color: str = "violet") -> dict[str, Any]:
        clean = " ".join(str(name or "").split())[:120]
        if not clean:
            raise ValueError("folder_name_required")
        folder_id, now = uuid.uuid4().hex, utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO job_folders(id,user_id,name,color,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (folder_id, int(user_id), clean, str(color or "violet")[:32], now, now),
            )
        return {"id": folder_id, "name": clean, "color": color, "job_count": 0, "created_at": now}

    def delete_folder(self, user_id: int, folder_id: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                "UPDATE job_library_meta SET folder_id='',updated_at=? WHERE user_id=? AND folder_id=?",
                (utc_now(), int(user_id), folder_id),
            )
            result = conn.execute("DELETE FROM job_folders WHERE user_id=? AND id=?", (int(user_id), folder_id))
        return int(getattr(result, "rowcount", 0) or 0) > 0

    def create_notification(self, user_id: int, *, event_key: str, kind: str, title: str,
                            body: str, job_id: str = "", severity: str = "info",
                            action_url: str = "") -> dict[str, Any] | None:
        item_id, now = uuid.uuid4().hex, utc_now()
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO notifications(id,user_id,event_key,kind,severity,title,body,job_id,
                    action_url,read_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,'',?)""",
                    (item_id, int(user_id), event_key[:190], kind[:48], severity[:24], title[:255],
                     body, job_id[:64], action_url[:512], now),
                )
        except Exception:
            return None
        return {"id": item_id, "kind": kind, "title": title, "body": body, "job_id": job_id,
                "severity": severity, "action_url": action_url, "read_at": "", "created_at": now}

    def list_notifications(self, user_id: int, *, limit: int = 100, unread_only: bool = False) -> list[dict[str, Any]]:
        where = "user_id=?" + (" AND read_at=''" if unread_only else "")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM notifications WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (int(user_id), max(1, min(500, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def unread_count(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count_value FROM notifications WHERE user_id=? AND read_at=''",
                (int(user_id),),
            ).fetchone()
        return int(dict(row).get("count_value") or 0) if row else 0

    def mark_notifications(self, user_id: int, notification_id: str = "") -> int:
        with self._connect() as conn:
            if notification_id:
                result = conn.execute(
                    "UPDATE notifications SET read_at=? WHERE user_id=? AND id=? AND read_at=''",
                    (utc_now(), int(user_id), notification_id),
                )
            else:
                result = conn.execute(
                    "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at=''",
                    (utc_now(), int(user_id)),
                )
        return int(getattr(result, "rowcount", 0) or 0)

    def get_preferences(self, user_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_preferences WHERE user_id=?", (int(user_id),)).fetchone()
        data = dict(row) if row else {"user_id": int(user_id), "browser_enabled": 1, "sound_enabled": 1,
                                     "line_enabled": 0, "email_enabled": 0, "line_target": "", "email_address": ""}
        for key in ("browser_enabled", "sound_enabled", "line_enabled", "email_enabled"):
            data[key] = bool(data.get(key))
        return data

    def save_preferences(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        current, now = self.get_preferences(user_id), utc_now()
        values = [int(bool(payload.get(key, current[key]))) for key in
                  ("browser_enabled", "sound_enabled", "line_enabled", "email_enabled")]
        line_target = str(payload.get("line_target", current.get("line_target", ""))).strip()[:255]
        email = str(payload.get("email_address", current.get("email_address", ""))).strip()[:255]
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM notification_preferences WHERE user_id=?", (int(user_id),)).fetchone()
            if exists:
                conn.execute(
                    """UPDATE notification_preferences SET browser_enabled=?,sound_enabled=?,line_enabled=?,
                    email_enabled=?,line_target=?,email_address=?,updated_at=? WHERE user_id=?""",
                    (*values, line_target, email, now, int(user_id)),
                )
            else:
                conn.execute(
                    """INSERT INTO notification_preferences(user_id,browser_enabled,sound_enabled,line_enabled,
                    email_enabled,line_target,email_address,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (int(user_id), *values, line_target, email, now),
                )
        return self.get_preferences(user_id)

    def list_devices(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,label,created_at,updated_at,last_seen_at FROM browser_identities WHERE user_id=? ORDER BY last_seen_at DESC",
                (int(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_device(self, user_id: int, device_id: int, label: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE browser_identities SET label=?,updated_at=? WHERE user_id=? AND id=?",
                (" ".join(str(label or "").split())[:255], utc_now(), int(user_id), int(device_id)),
            )
        return int(getattr(result, "rowcount", 0) or 0) > 0

    def revoke_device(self, user_id: int, device_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute("DELETE FROM browser_identities WHERE user_id=? AND id=?", (int(user_id), int(device_id)))
        return int(getattr(result, "rowcount", 0) or 0) > 0

    def create_recovery_code(self, user_id: int, *, days: int = 30) -> dict[str, Any]:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(20))
        code = "SJ88-" + "-".join(raw[index:index + 4] for index in range(0, 20, 4))
        digest = hmac.new(BROWSER_IDENTITY_SECRET.encode(), code.encode(), hashlib.sha256).hexdigest()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=max(1, min(90, int(days))))
        with self._connect() as conn:
            conn.execute("UPDATE guest_recovery_codes SET used_at=? WHERE user_id=? AND used_at=''", (now.isoformat(), int(user_id)))
            conn.execute(
                "INSERT INTO guest_recovery_codes(id,user_id,code_hash,expires_at,used_at,created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, int(user_id), digest, expires.isoformat(), "", now.isoformat()),
            )
        return {"code": code, "expires_at": expires.isoformat()}

    def consume_recovery_code(self, code: str) -> int:
        clean = str(code or "").strip().upper()
        digest = hmac.new(BROWSER_IDENTITY_SECRET.encode(), clean.encode(), hashlib.sha256).hexdigest()
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM guest_recovery_codes WHERE code_hash=? AND used_at='' AND expires_at>? LIMIT 1",
                (digest, now),
            ).fetchone()
            if not row:
                return 0
            data = dict(row)
            conn.execute("UPDATE guest_recovery_codes SET used_at=? WHERE id=?", (now, data["id"]))
        return int(data.get("user_id") or 0)
