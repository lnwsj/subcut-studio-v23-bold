"""Browser-bound guest accounts and later account claiming.

A browser key is stored in the browser profile and in a SameSite cookie. The
server stores only an HMAC digest of that key. Every key maps to a real user
row, so guest jobs continue running and remain visible after a page reload.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

from ..config import BROWSER_IDENTITY_SECRET
from .auth_service import AuthService, _utc_now_iso
from .db import _connect, is_mysql_backend

_BROWSER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


class BrowserAuthService:
    """Create, restore, claim, and merge browser-bound accounts."""

    cookie_name = "sj88_browser_key"
    cookie_max_age = 63_072_000  # two years

    def __init__(self, db_path: Path, auth: AuthService) -> None:
        self.db_path = Path(db_path)
        self.auth = auth
        self._ensure_schema()

    def _connect(self):
        return _connect(self.db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            if is_mysql_backend():
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS browser_identities (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        browser_hash CHAR(64) NOT NULL UNIQUE,
                        user_id BIGINT UNSIGNED NOT NULL,
                        label VARCHAR(255) NOT NULL DEFAULT '',
                        created_at VARCHAR(64) NOT NULL,
                        updated_at VARCHAR(64) NOT NULL,
                        last_seen_at VARCHAR(64) NOT NULL,
                        INDEX idx_browser_identity_user (user_id),
                        CONSTRAINT fk_browser_identity_user
                            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS browser_identities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        browser_hash TEXT NOT NULL UNIQUE,
                        user_id INTEGER NOT NULL,
                        label TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_browser_identity_user "
                    "ON browser_identities(user_id)"
                )

    @staticmethod
    def normalize_key(value: Any) -> str:
        key = str(value or "").strip()
        return key if _BROWSER_KEY_RE.fullmatch(key) else ""

    @staticmethod
    def new_key() -> str:
        return secrets.token_urlsafe(40)

    @staticmethod
    def is_guest(row: dict[str, Any] | None) -> bool:
        if not row:
            return False
        role = str(row.get("role") or "").strip().lower()
        source = str(row.get("signup_source") or "").strip().lower()
        return role == "guest" or source == "browser_guest"

    def _hash_key(self, browser_key: str) -> str:
        return hmac.new(
            BROWSER_IDENTITY_SECRET.encode("utf-8"),
            browser_key.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _identity(self, browser_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM browser_identities WHERE browser_hash = ? LIMIT 1",
                (browser_hash,),
            ).fetchone()
        return dict(row) if row else None

    def _touch_identity(self, browser_hash: str, *, label: str = "") -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE browser_identities
                SET label = CASE WHEN ? <> '' THEN ? ELSE label END,
                    updated_at = ?, last_seen_at = ?
                WHERE browser_hash = ?
                """,
                (label, label, now, now, browser_hash),
            )

    def _bind_identity(self, browser_hash: str, user_id: int, *, label: str = "") -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE browser_identities
                SET user_id = ?, label = ?, updated_at = ?, last_seen_at = ?
                WHERE browser_hash = ?
                """,
                (int(user_id), label[:255], now, now, browser_hash),
            )
            if int(getattr(updated, "rowcount", 0) or 0) > 0:
                return
            try:
                conn.execute(
                    """
                    INSERT INTO browser_identities(
                        browser_hash, user_id, label, created_at, updated_at, last_seen_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (browser_hash, int(user_id), label[:255], now, now, now),
                )
            except Exception:
                conn.execute(
                    """
                    UPDATE browser_identities
                    SET user_id = ?, label = ?, updated_at = ?, last_seen_at = ?
                    WHERE browser_hash = ?
                    """,
                    (int(user_id), label[:255], now, now, browser_hash),
                )

    def _create_guest(self, browser_hash: str, *, label: str = "") -> dict[str, Any]:
        email = f"guest-{browser_hash[:32]}@browser.local"
        existing = self.auth._get_user_by_email(email)
        if existing:
            return existing
        try:
            return self.auth._create_user(
                email=email,
                password_hash="!",
                display_name=f"Chrome Guest {browser_hash[:4].upper()}",
                role="guest",
                plan="free",
                account_status="approved",
                signup_source="browser_guest",
                approved_at=_utc_now_iso(),
                approval_note=label[:255],
            )
        except Exception:
            existing = self.auth._get_user_by_email(email)
            if existing:
                return existing
            raise

    def ensure_session(
        self,
        browser_key: Any = "",
        *,
        label: str = "",
        force_new: bool = False,
    ) -> dict[str, Any]:
        """Return tokens for the account bound to this browser key."""
        key = "" if force_new else self.normalize_key(browser_key)
        if not key:
            key = self.new_key()
        browser_hash = self._hash_key(key)
        identity = self._identity(browser_hash)
        user = self.auth._get_user_by_id(int(identity.get("user_id") or 0)) if identity else None
        if not user:
            user = self._create_guest(browser_hash, label=label)
            self._bind_identity(browser_hash, int(user["id"]), label=label)
        else:
            self._touch_identity(browser_hash, label=label)

        result = self.auth._issue_tokens(user)
        result.update(
            {
                "browser_key": key,
                "browser_bound": True,
                "is_guest": self.is_guest(user),
            }
        )
        return result

    def _registered_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count_value FROM users
                WHERE role <> 'guest' AND signup_source <> 'browser_guest'
                """
            ).fetchone()
        return int(dict(row).get("count_value") or 0) if row else 0

    def _jobs_count(self, user_id: int) -> int:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count_value FROM jobs WHERE user_id = ?",
                    (int(user_id),),
                ).fetchone()
            return int(dict(row).get("count_value") or 0) if row else 0
        except Exception:
            return 0

    def claim_guest(
        self,
        *,
        browser_key: Any,
        current_user_id: int,
        email: str,
        password: str,
        display_name: str,
    ) -> dict[str, Any]:
        """Upgrade the same guest row, preserving every job and workspace."""
        key = self.normalize_key(browser_key)
        normalized_email = self.auth._normalize_email(email)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized_email):
            return {"ok": False, "error": "Invalid email"}
        if len(password or "") < 8:
            return {"ok": False, "error": "Password must be at least 8 characters"}

        identity = self._identity(self._hash_key(key)) if key else None
        identity_user_id = int(identity.get("user_id") or 0) if identity else 0
        guest_id = int(current_user_id or identity_user_id or 0)
        guest = self.auth._get_user_by_id(guest_id)
        if not self.is_guest(guest):
            return {"ok": False, "error": "guest_session_required"}
        duplicate = self.auth._get_user_by_email(normalized_email)
        if duplicate and int(duplicate.get("id") or 0) != guest_id:
            return {"ok": False, "error": "This email is already registered"}

        first_member = self._registered_count() == 0
        now = _utc_now_iso()
        jobs_preserved = self._jobs_count(guest_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET email = ?, password_hash = ?, display_name = ?, role = ?,
                    account_status = 'approved', signup_source = 'browser_claim',
                    approved_at = ?, approved_by = 0, approval_note = '',
                    status_updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_email,
                    self.auth._hash_password(password),
                    self.auth._normalize_name(display_name, normalized_email),
                    "owner" if first_member else "user",
                    now,
                    now,
                    guest_id,
                ),
            )
            conn.execute(
                "UPDATE auth_refresh_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at = ''",
                (now, guest_id),
            )
        updated = self.auth._get_user_by_id(guest_id)
        if not updated:
            return {"ok": False, "error": "account_claim_failed"}
        if key:
            self._bind_identity(self._hash_key(key), guest_id, label="claimed member")
        result = self.auth._issue_tokens(updated)
        result.update(
            {
                "upgraded_from_guest": True,
                "jobs_preserved": jobs_preserved,
                "browser_key": key,
                "browser_bound": bool(key),
            }
        )
        return result

    def _merge_guest_data(self, guest_id: int, target_id: int) -> int:
        if guest_id <= 0 or target_id <= 0 or guest_id == target_id:
            return 0
        migrated = self._jobs_count(guest_id)
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET user_id = ? WHERE user_id = ?", (target_id, guest_id))
            try:
                conn.execute("UPDATE abroll_jobs SET user_id = ? WHERE user_id = ?", (target_id, guest_id))
            except Exception:
                pass
            try:
                source_settings = conn.execute(
                    "SELECT settings_json FROM subtitle_trim_settings WHERE user_id = ? LIMIT 1",
                    (guest_id,),
                ).fetchone()
                target_settings = conn.execute(
                    "SELECT 1 FROM subtitle_trim_settings WHERE user_id = ? LIMIT 1",
                    (target_id,),
                ).fetchone()
                if source_settings and not target_settings:
                    conn.execute(
                        "UPDATE subtitle_trim_settings SET user_id = ? WHERE user_id = ?",
                        (target_id, guest_id),
                    )
                elif source_settings:
                    conn.execute("DELETE FROM subtitle_trim_settings WHERE user_id = ?", (guest_id,))
            except Exception:
                pass
            conn.execute(
                "UPDATE browser_identities SET user_id = ?, updated_at = ? WHERE user_id = ?",
                (target_id, now, guest_id),
            )
            conn.execute(
                "UPDATE auth_refresh_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at = ''",
                (now, guest_id),
            )
            conn.execute(
                """
                UPDATE users
                SET account_status = 'disabled', status_updated_at = ?,
                    approval_note = ?
                WHERE id = ?
                """,
                (now, f"merged_into:{target_id}", guest_id),
            )
        return migrated

    def login_and_link(
        self,
        *,
        browser_key: Any,
        current_user_id: int,
        email: str,
        password: str,
    ) -> dict[str, Any]:
        """Authenticate a member and attach any current guest jobs to it."""
        normalized_email = self.auth._normalize_email(email)
        target = self.auth._get_user_by_email(normalized_email)
        if not target or not self.auth._verify_password(password, str(target.get("password_hash") or "")):
            return {"ok": False, "error": "Invalid email or password"}
        account_error = self.auth._account_error(target)
        if account_error:
            return {"ok": False, "error": account_error, "user": self.auth._public_user(target)}

        key = self.normalize_key(browser_key)
        identity = self._identity(self._hash_key(key)) if key else None
        source_id = int(current_user_id or (identity.get("user_id") if identity else 0) or 0)
        source = self.auth._get_user_by_id(source_id) if source_id else None
        target_id = int(target.get("id") or 0)
        migrated = self._merge_guest_data(source_id, target_id) if self.is_guest(source) else 0
        if key:
            self._bind_identity(self._hash_key(key), target_id, label="member login")

        fresh_target = self.auth._get_user_by_id(target_id) or target
        result = self.auth._issue_tokens(fresh_target)
        result.update(
            {
                "guest_jobs_migrated": migrated,
                "browser_key": key,
                "browser_bound": bool(key),
            }
        )
        return result

    def link_authenticated_user(
        self,
        *,
        browser_key: Any,
        current_user_id: int,
        target_user_id: int,
    ) -> dict[str, Any]:
        """Bind an SSO-authenticated user and merge current guest jobs."""
        key = self.normalize_key(browser_key)
        source = self.auth._get_user_by_id(int(current_user_id or 0))
        migrated = 0
        if self.is_guest(source):
            migrated = self._merge_guest_data(int(source["id"]), int(target_user_id))
        if key:
            self._bind_identity(self._hash_key(key), int(target_user_id), label="sso login")
        target = self.auth._get_user_by_id(int(target_user_id))
        if not target:
            return {"ok": False, "error": "member_not_found"}
        result = self.auth._issue_tokens(target)
        result.update(
            {
                "guest_jobs_migrated": migrated,
                "browser_key": key,
                "browser_bound": bool(key),
            }
        )
        return result

    def bind_existing_user(self, browser_key: Any, user_id: int, *, label: str = "recovered device") -> dict[str, Any]:
        """Bind a validated recovery target to the current browser and issue tokens."""
        key = self.normalize_key(browser_key) or self.new_key()
        user = self.auth._get_user_by_id(int(user_id))
        if not user:
            return {"ok": False, "error": "recovery_user_not_found"}
        self._bind_identity(self._hash_key(key), int(user_id), label=label)
        result = self.auth._issue_tokens(user)
        result.update({"browser_key": key, "browser_bound": True, "recovered": True})
        return result
