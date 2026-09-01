"""Authentication and class-ticket SSO service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Request

from ..config import (
    AUTH_ACCESS_TTL_SEC,
    AUTH_REFRESH_TTL_SEC,
    AUTH_SECRET,
    CLASS_SSO_EXCHANGE_URL,
    CLASS_SSO_SHARED_SECRET,
    CLASS_SSO_TIMEOUT_SEC,
)
from .db import _connect, is_mysql_backend


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_duplicate_column_error(exc: Exception) -> bool:
    t = str(exc).lower()
    return "duplicate column" in t or "duplicate column name" in t or "1060" in t


class AuthService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if not is_mysql_backend():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        return _connect(self.db_path)

    def _ensure_schema(self) -> None:
        if is_mysql_backend():
            self._ensure_schema_mysql()
        else:
            self._ensure_schema_sqlite()

    def _ensure_schema_sqlite(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    plan TEXT NOT NULL DEFAULT 'free',
                    account_status TEXT NOT NULL DEFAULT 'approved',
                    signup_source TEXT NOT NULL DEFAULT 'migration',
                    approved_at TEXT NOT NULL DEFAULT '',
                    approved_by INTEGER NOT NULL DEFAULT 0,
                    approval_note TEXT NOT NULL DEFAULT '',
                    status_updated_at TEXT NOT NULL DEFAULT '',
                    external_provider TEXT NOT NULL DEFAULT '',
                    external_subject TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_external ON users(external_provider, external_subject)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_refresh_user ON auth_refresh_tokens(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_refresh_exp ON auth_refresh_tokens(expires_at)")

            for col, dtype in [
                ("display_name", "TEXT NOT NULL DEFAULT ''"),
                ("role", "TEXT NOT NULL DEFAULT 'user'"),
                ("plan", "TEXT NOT NULL DEFAULT 'free'"),
                ("account_status", "TEXT NOT NULL DEFAULT 'approved'"),
                ("signup_source", "TEXT NOT NULL DEFAULT 'migration'"),
                ("approved_at", "TEXT NOT NULL DEFAULT ''"),
                ("approved_by", "INTEGER NOT NULL DEFAULT 0"),
                ("approval_note", "TEXT NOT NULL DEFAULT ''"),
                ("status_updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("external_provider", "TEXT NOT NULL DEFAULT ''"),
                ("external_subject", "TEXT NOT NULL DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {dtype}")
                except Exception:
                    pass

    def _ensure_schema_mysql(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    display_name VARCHAR(255) NOT NULL DEFAULT '',
                    role VARCHAR(64) NOT NULL DEFAULT 'user',
                    plan VARCHAR(64) NOT NULL DEFAULT 'free',
                    account_status VARCHAR(32) NOT NULL DEFAULT 'approved',
                    signup_source VARCHAR(64) NOT NULL DEFAULT 'migration',
                    approved_at VARCHAR(64) NOT NULL DEFAULT '',
                    approved_by BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    approval_note TEXT NULL,
                    status_updated_at VARCHAR(64) NOT NULL DEFAULT '',
                    external_provider VARCHAR(64) NOT NULL DEFAULT '',
                    external_subject VARCHAR(255) NOT NULL DEFAULT '',
                    created_at VARCHAR(64) NOT NULL,
                    INDEX idx_users_external (external_provider, external_subject)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT UNSIGNED NOT NULL,
                    token_hash CHAR(64) NOT NULL UNIQUE,
                    expires_at VARCHAR(64) NOT NULL,
                    revoked_at VARCHAR(64) NOT NULL DEFAULT '',
                    created_at VARCHAR(64) NOT NULL,
                    INDEX idx_auth_refresh_user (user_id),
                    INDEX idx_auth_refresh_exp (expires_at),
                    CONSTRAINT fk_auth_refresh_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            for col, dtype in [
                ("display_name", "VARCHAR(255) NOT NULL DEFAULT ''"),
                ("role", "VARCHAR(64) NOT NULL DEFAULT 'user'"),
                ("plan", "VARCHAR(64) NOT NULL DEFAULT 'free'"),
                ("account_status", "VARCHAR(32) NOT NULL DEFAULT 'approved'"),
                ("signup_source", "VARCHAR(64) NOT NULL DEFAULT 'migration'"),
                ("approved_at", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("approved_by", "BIGINT UNSIGNED NOT NULL DEFAULT 0"),
                ("approval_note", "TEXT NULL"),
                ("status_updated_at", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("external_provider", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("external_subject", "VARCHAR(255) NOT NULL DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {dtype}")
                except Exception as exc:
                    if not _is_duplicate_column_error(exc):
                        raise

    @staticmethod
    def _normalize_email(email: str) -> str:
        return str(email or "").strip().lower()

    @staticmethod
    def _normalize_name(name: str, fallback_email: str = "") -> str:
        candidate = str(name or "").strip()
        if candidate:
            return candidate[:255]
        if fallback_email:
            return fallback_email.split("@")[0][:255]
        return "User"

    @staticmethod
    def _hash_password(password: str) -> str:
        iterations = 200_000
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        try:
            parts = str(stored or "").split("$")
            if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
                return False
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    @staticmethod
    def _normalize_account_status(value: str | None) -> str:
        status = str(value or "").strip().lower()
        return status if status in {"approved", "pending", "rejected", "disabled"} else "approved"

    @classmethod
    def _account_status(cls, row: dict[str, Any]) -> str:
        return cls._normalize_account_status(str(row.get("account_status") or "approved"))

    @classmethod
    def _account_error(cls, row: dict[str, Any]) -> str:
        status = cls._account_status(row)
        if status == "pending":
            return "account_pending"
        if status == "rejected":
            return "account_rejected"
        if status == "disabled":
            return "account_disabled"
        return ""

    @classmethod
    def _public_user(cls, row: dict[str, Any]) -> dict[str, Any]:
        role = str(row.get("role") or "user")
        signup_source = str(row.get("signup_source") or "migration")
        return {
            "id": int(row.get("id") or 0),
            "email": str(row.get("email") or ""),
            "display_name": str(row.get("display_name") or row.get("name") or ""),
            "role": role,
            "plan": str(row.get("plan") or "free"),
            "account_status": cls._account_status(row),
            "signup_source": signup_source,
            "is_guest": role.strip().lower() == "guest" or signup_source.strip().lower() == "browser_guest",
        }

    def public_user(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._public_user(row)

    def _sign_access_token(self, payload: dict[str, Any]) -> str:
        body = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        sig = hmac.new(AUTH_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64url_encode(sig)}"

    def _decode_access_token(self, token: str) -> dict[str, Any] | None:
        token = str(token or "").strip()
        if "." not in token:
            return None
        body_b64, sig_b64 = token.split(".", 1)
        expected = _b64url_encode(hmac.new(AUTH_SECRET.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig_b64):
            return None
        try:
            payload = json.loads(_b64url_decode(body_b64).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("typ") != "access":
            return None
        exp = int(payload.get("exp") or 0)
        if exp <= int(_utc_now().timestamp()):
            return None
        return payload

    def _issue_tokens(self, user_row: dict[str, Any]) -> dict[str, Any]:
        account_error = self._account_error(user_row)
        if account_error:
            return {"ok": False, "error": account_error, "user": self._public_user(user_row)}

        now = _utc_now()
        access_exp = now + timedelta(seconds=max(60, AUTH_ACCESS_TTL_SEC))
        refresh_exp = now + timedelta(seconds=max(300, AUTH_REFRESH_TTL_SEC))
        public_user = self._public_user(user_row)

        access_payload = {
            "typ": "access",
            "sub": public_user["id"],
            "email": public_user["email"],
            "display_name": public_user["display_name"],
            "role": public_user["role"],
            "plan": public_user["plan"],
            "iat": int(now.timestamp()),
            "exp": int(access_exp.timestamp()),
        }
        access_token = self._sign_access_token(access_payload)

        refresh_token = secrets.token_hex(48)
        token_hash = _sha256_hex(refresh_token)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth_refresh_tokens(user_id, token_hash, expires_at, revoked_at, created_at) VALUES(?, ?, ?, '', ?)",
                (public_user["id"], token_hash, refresh_exp.isoformat(), now.isoformat()),
            )

        return {
            "ok": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": public_user,
            "expires_in": int(max(60, AUTH_ACCESS_TTL_SEC)),
        }

    def _get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return dict(row) if row else None

    def _get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ? LIMIT 1", (email,)).fetchone()
        return dict(row) if row else None

    def _user_count(self) -> int:
        """Return the current user count for safe first-account bootstrap."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count_value FROM users "
                "WHERE role <> 'guest' AND signup_source <> 'browser_guest'"
            ).fetchone()
        if not row:
            return 0
        try:
            return int(row["count_value"] if isinstance(row, dict) else row[0])
        except Exception:
            return 0

    def list_members(
        self,
        *,
        limit: int = 200,
        status: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        """List member accounts for owner/admin management."""
        safe_limit = max(1, min(500, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, email, display_name, role, plan, account_status,
                       signup_source, approved_at, approved_by, approval_note,
                       status_updated_at, created_at
                FROM users
                WHERE role <> 'guest' AND signup_source <> 'browser_guest'
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        wanted_status = str(status or "").strip().lower()
        search = str(query or "").strip().lower()
        output: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            normalized_status = self._account_status(row)
            if wanted_status and wanted_status != "all" and normalized_status != wanted_status:
                continue
            haystack = f"{row.get('display_name', '')} {row.get('email', '')}".lower()
            if search and search not in haystack:
                continue
            member = self._public_user(row)
            member.update(
                {
                    "created_at": str(row.get("created_at") or ""),
                    "approved_at": str(row.get("approved_at") or ""),
                    "status_updated_at": str(row.get("status_updated_at") or ""),
                    "approval_note": str(row.get("approval_note") or ""),
                }
            )
            output.append(member)
        return output

    def set_member_status(
        self,
        *,
        actor_id: int,
        user_id: int,
        status: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Update approval state while protecting the current and last owner."""
        normalized = self._normalize_account_status(status)
        if normalized != str(status or "").strip().lower():
            return {"ok": False, "error": "invalid_account_status"}
        target = self._get_user_by_id(int(user_id))
        if not target:
            return {"ok": False, "error": "member_not_found"}
        if int(actor_id) == int(user_id) and normalized != "approved":
            return {"ok": False, "error": "cannot_suspend_current_account"}

        target_role = str(target.get("role") or "user").strip().lower()
        if target_role in {"owner", "superadmin"} and normalized != "approved":
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count_value
                    FROM users
                    WHERE role IN ('owner', 'superadmin') AND account_status = 'approved'
                    """
                ).fetchone()
            count_value = int(dict(row).get("count_value") or 0) if row else 0
            if count_value <= 1:
                return {"ok": False, "error": "cannot_suspend_last_owner"}

        now = _utc_now_iso()
        clean_note = str(note or "").strip()[:1000]
        approved_at = now if normalized == "approved" else str(target.get("approved_at") or "")
        approved_by = int(actor_id) if normalized == "approved" else int(target.get("approved_by") or 0)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET account_status = ?, approved_at = ?, approved_by = ?,
                    approval_note = ?, status_updated_at = ?
                WHERE id = ?
                """,
                (normalized, approved_at, approved_by, clean_note, now, int(user_id)),
            )
            if normalized != "approved":
                conn.execute(
                    "UPDATE auth_refresh_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at = ''",
                    (now, int(user_id)),
                )
        updated = self._get_user_by_id(int(user_id))
        return {"ok": True, "user": self._public_user(updated or target)}

    def _get_user_by_external(self, provider: str, subject: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE external_provider = ? AND external_subject = ? LIMIT 1",
                (provider, subject),
            ).fetchone()
        return dict(row) if row else None

    def _create_user(
        self,
        *,
        email: str,
        password_hash: str,
        display_name: str,
        role: str = "user",
        plan: str = "free",
        external_provider: str = "",
        external_subject: str = "",
        account_status: str = "approved",
        signup_source: str = "migration",
        approved_at: str = "",
        approved_by: int = 0,
        approval_note: str = "",
    ) -> dict[str, Any]:
        created = _utc_now_iso()
        status = self._normalize_account_status(account_status)
        status_updated_at = created
        with self._connect() as conn:
            result = conn.execute(
                """
                INSERT INTO users(
                    email, password_hash, display_name, role, plan,
                    account_status, signup_source, approved_at, approved_by, approval_note, status_updated_at,
                    external_provider, external_subject, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    password_hash,
                    display_name,
                    role,
                    plan,
                    status,
                    signup_source,
                    approved_at,
                    int(approved_by or 0),
                    approval_note,
                    status_updated_at,
                    external_provider,
                    external_subject,
                    created,
                ),
            )
            user_id = int(result.lastrowid or 0)
            row = conn.execute("SELECT * FROM users WHERE id = ? LIMIT 1", (user_id,)).fetchone()
        if not row:
            raise RuntimeError("failed to create user")
        return dict(row)

    def register(self, email: str, password: str, display_name: str) -> dict[str, Any]:
        normalized_email = self._normalize_email(email)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized_email):
            return {"ok": False, "error": "Invalid email"}
        if len(password or "") < 8:
            return {"ok": False, "error": "Password must be at least 8 characters"}
        if self._get_user_by_email(normalized_email):
            return {"ok": False, "error": "This email is already registered"}

        # A fresh standalone install must be usable without editing the DB by
        # hand. The very first account becomes the approved owner. Existing
        # deployments keep the original approval workflow for every new user.
        first_account = self._user_count() == 0
        now = _utc_now_iso()
        row = self._create_user(
            email=normalized_email,
            password_hash=self._hash_password(password),
            display_name=self._normalize_name(display_name, normalized_email),
            role="owner" if first_account else "user",
            account_status="approved" if first_account else "pending",
            signup_source="local",
            approved_at=now if first_account else "",
        )
        if first_account:
            return self._issue_tokens(row)
        return {
            "ok": True,
            "pending_approval": True,
            "message": "account_pending_approval",
            "user": self._public_user(row),
        }

    def login(self, email: str, password: str) -> dict[str, Any]:
        normalized_email = self._normalize_email(email)
        row = self._get_user_by_email(normalized_email)
        if not row:
            return {"ok": False, "error": "Invalid email or password"}
        if not self._verify_password(password, str(row.get("password_hash") or "")):
            return {"ok": False, "error": "Invalid email or password"}
        account_error = self._account_error(row)
        if account_error:
            return {"ok": False, "error": account_error, "user": self._public_user(row)}
        return self._issue_tokens(row)

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        token_hash = _sha256_hex(str(refresh_token or ""))
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    rt.id, rt.user_id, rt.expires_at, rt.revoked_at,
                    u.id AS uid, u.email, u.display_name, u.role, u.plan,
                    u.account_status, u.signup_source
                FROM auth_refresh_tokens rt
                JOIN users u ON u.id = rt.user_id
                WHERE rt.token_hash = ?
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "Invalid refresh token"}
            record = dict(row)
            if str(record.get("revoked_at") or "").strip():
                return {"ok": False, "error": "Refresh token revoked"}
            exp = _parse_utc(str(record.get("expires_at") or ""))
            if not exp or exp <= _utc_now():
                return {"ok": False, "error": "Refresh token expired"}

            # Rotate refresh token.
            conn.execute("UPDATE auth_refresh_tokens SET revoked_at = ? WHERE id = ?", (_utc_now_iso(), int(record["id"])))

            user_row = {
                "id": int(record["uid"]),
                "email": record.get("email") or "",
                "display_name": record.get("display_name") or "",
                "role": record.get("role") or "user",
                "plan": record.get("plan") or "free",
                "account_status": record.get("account_status") or "approved",
                "signup_source": record.get("signup_source") or "migration",
            }
        account_error = self._account_error(user_row)
        if account_error:
            self.logout(refresh_token)
            return {"ok": False, "error": account_error, "user": self._public_user(user_row)}
        return self._issue_tokens(user_row)

    def logout(self, refresh_token: str) -> dict[str, Any]:
        token_hash = _sha256_hex(str(refresh_token or ""))
        with self._connect() as conn:
            conn.execute(
                "UPDATE auth_refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at = ''",
                (_utc_now_iso(), token_hash),
            )
        return {"ok": True}

    def resolve_user_from_request(self, request: Request) -> dict[str, Any] | None:
        token = self.extract_access_token(request)
        if not token:
            return None
        payload = self._decode_access_token(token)
        if not payload:
            return None
        user_id = int(payload.get("sub") or 0)
        if user_id <= 0:
            return None
        user = self._get_user_by_id(user_id)
        if not user or self._account_error(user):
            return None
        return user

    @staticmethod
    def extract_access_token(request: Request) -> str:
        auth_header = str(request.headers.get("authorization", "")).strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        query_token = str(request.query_params.get("token") or request.query_params.get("t") or "").strip()
        return query_token

    def exchange_class_sso_ticket(self, ticket: str) -> dict[str, Any]:
        ticket = str(ticket or "").strip().lower()
        if not re.match(r"^[a-f0-9]{64}$", ticket):
            return {"ok": False, "error": "invalid_ticket_format"}
        if not CLASS_SSO_EXCHANGE_URL:
            return {"ok": False, "error": "sso_exchange_url_not_configured"}

        headers = {"Content-Type": "application/json"}
        if CLASS_SSO_SHARED_SECRET:
            headers["X-SSO-Secret"] = CLASS_SSO_SHARED_SECRET

        try:
            resp = requests.post(
                CLASS_SSO_EXCHANGE_URL,
                json={"ticket": ticket},
                headers=headers,
                timeout=max(2.0, float(CLASS_SSO_TIMEOUT_SEC)),
            )
        except Exception:
            return {"ok": False, "error": "class_sso_unreachable"}

        try:
            payload = resp.json()
        except Exception:
            payload = {}

        if resp.status_code != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
            detail = str(payload.get("err") or f"class_sso_http_{resp.status_code}")
            return {"ok": False, "error": detail}

        class_uid = int(payload.get("user_id") or 0)
        if class_uid <= 0:
            return {"ok": False, "error": "class_sso_missing_user_id"}

        provider = "class_lnw"
        subject = str(class_uid)
        email = self._normalize_email(str(payload.get("email") or ""))
        if not email:
            email = f"class-{subject}@class.lnwsj.local"
        display_name = self._normalize_name(str(payload.get("name") or ""), email)
        role = str(payload.get("role") or "student").strip().lower() or "student"
        local_role = "admin" if role in {"admin", "owner", "superadmin"} else "user"

        row = self._get_user_by_external(provider, subject)
        with self._connect() as conn:
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET email = ?, display_name = ?, role = ?,
                        account_status = 'approved', signup_source = ?,
                        approved_at = CASE WHEN approved_at = '' THEN ? ELSE approved_at END,
                        status_updated_at = ?
                    WHERE id = ?
                    """,
                    (email, display_name, local_role, provider, _utc_now_iso(), _utc_now_iso(), int(row["id"])),
                )
                user_id = int(row["id"])
            else:
                row_by_email = self._get_user_by_email(email)
                if row_by_email:
                    return {"ok": False, "error": "email_already_registered"}
                else:
                    result = conn.execute(
                        """
                        INSERT INTO users(
                            email, password_hash, display_name, role, plan,
                            account_status, signup_source, approved_at, approved_by, approval_note, status_updated_at,
                            external_provider, external_subject, created_at
                        )
                        VALUES(?, ?, ?, ?, 'free', 'approved', ?, ?, 0, '', ?, ?, ?, ?)
                        """,
                        (email, "!", display_name, local_role, provider, _utc_now_iso(), _utc_now_iso(), provider, subject, _utc_now_iso()),
                    )
                    user_id = int(result.lastrowid or 0)

        user_row = self._get_user_by_id(user_id)
        if not user_row:
            return {"ok": False, "error": "failed_to_create_local_user"}
        return self._issue_tokens(user_row)
