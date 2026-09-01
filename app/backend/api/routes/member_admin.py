"""Focused owner/admin member-management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ._auth_guard import require_admin_user, user_id_from_user
from ...config import DB_PATH
from ...services.auth_service import AuthService

router = APIRouter(prefix="/api/members", tags=["Members"])
_auth = AuthService(DB_PATH)
_ACTION_STATUS = {
    "approve": "approved",
    "restore": "approved",
    "pending": "pending",
    "reject": "rejected",
    "disable": "disabled",
}


@router.get("")
def list_members(
    status: str = Query("all"),
    q: str = Query("", max_length=120),
    limit: int = Query(200, ge=1, le=500),
    _admin: dict[str, Any] = Depends(require_admin_user),
) -> dict[str, Any]:
    members = _auth.list_members(limit=limit, status=status, query=q)
    return {
        "ok": True,
        "members": members,
        "total": len(members),
        "pending": sum(1 for item in members if item.get("account_status") == "pending"),
    }


@router.patch("/{user_id}")
def update_member(
    user_id: int,
    payload: dict[str, Any],
    admin: dict[str, Any] = Depends(require_admin_user),
) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    status = _ACTION_STATUS.get(action, str(payload.get("status") or "").strip().lower())
    result = _auth.set_member_status(
        actor_id=user_id_from_user(admin),
        user_id=user_id,
        status=status,
        note=str(payload.get("note") or ""),
    )
    if not result.get("ok"):
        error = str(result.get("error") or "member_update_failed")
        code = 404 if error == "member_not_found" else 409
        raise HTTPException(status_code=code, detail=error)
    return result
