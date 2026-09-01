"""Shared auth dependency helpers for API routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from ...config import AUTH_REQUIRED, DB_PATH
from ...services.auth_service import AuthService

_auth = AuthService(DB_PATH)
ADMIN_ROLES = {"admin", "owner", "superadmin"}


def resolve_request_user(request: Request) -> dict[str, Any] | None:
    user = _auth.resolve_user_from_request(request)
    if not user:
        return None
    return _auth.public_user(user)


def require_request_user(request: Request) -> dict[str, Any]:
    user = resolve_request_user(request)
    if user:
        return user
    if AUTH_REQUIRED:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {
        "id": 0,
        "email": "guest@local",
        "display_name": "Guest",
        "role": "guest",
        "plan": "free",
    }


def require_admin_user(request: Request) -> dict[str, Any]:
    user = require_request_user(request)
    role = str(user.get("role") or "").strip().lower()
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def user_id_from_user(user: dict[str, Any]) -> int:
    try:
        value = int(user.get("id") or 0)
    except Exception:
        value = 0
    if value < 0:
        return 0
    return value
