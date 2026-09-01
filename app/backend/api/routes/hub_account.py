"""Notification Center, device sync, and guest recovery APIs."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ._auth_guard import require_request_user, user_id_from_user
from .hub_utils import WORKSPACE
from ...config import DB_PATH
from ...services.auth_service import AuthService
from ...services.browser_auth_service import BrowserAuthService

router = APIRouter(tags=["Notifications and Devices"])
AUTH = AuthService(DB_PATH)
BROWSER = BrowserAuthService(DB_PATH, AUTH)


def _browser_key(payload: dict[str, Any], request: Request) -> str:
    supplied = payload.get("browser_key") or payload.get("device_token")
    return BROWSER.normalize_key(supplied or request.cookies.get(BROWSER.cookie_name, ""))


def _set_cookie(response: Response, request: Request, key: str) -> None:
    response.set_cookie(
        key=BROWSER.cookie_name, value=key, max_age=BROWSER.cookie_max_age,
        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/",
    )


@router.get("/api/notifications")
def list_notifications(unread_only: bool = False, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    items = WORKSPACE.list_notifications(user_id, unread_only=unread_only)
    return {"ok": True, "items": items, "unread": WORKSPACE.unread_count(user_id)}


@router.post("/api/notifications/{notification_id}/read")
def mark_notification(notification_id: str, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    changed = WORKSPACE.mark_notifications(user_id, notification_id)
    return {"ok": True, "changed": changed, "unread": WORKSPACE.unread_count(user_id)}


@router.post("/api/notifications/read-all")
def mark_all_notifications(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    changed = WORKSPACE.mark_notifications(user_id)
    return {"ok": True, "changed": changed, "unread": 0}


@router.post("/api/notifications/test")
def test_notification(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    item = WORKSPACE.create_notification(
        user_id, event_key=f"test:{time.time_ns()}", kind="test", severity="success",
        title="ทดสอบ Notification Center สำเร็จ", body="อุปกรณ์นี้เชื่อมต่อและซิงก์การแจ้งเตือนได้ตามปกติ",
        action_url="/#notifications",
    )
    return {"ok": True, "item": item}


@router.get("/api/notification-preferences")
def get_notification_preferences(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    return {"ok": True, "preferences": WORKSPACE.get_preferences(user_id_from_user(current_user))}


@router.patch("/api/notification-preferences")
def update_notification_preferences(payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    preferences = WORKSPACE.save_preferences(user_id_from_user(current_user), payload if isinstance(payload, dict) else {})
    return {"ok": True, "preferences": preferences}


@router.get("/api/devices")
def list_devices(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    return {"ok": True, "items": WORKSPACE.list_devices(user_id), "sync_scope": "account" if not current_user.get("is_guest") else "browser+recovery"}


@router.patch("/api/devices/{device_id}")
def rename_device(device_id: int, payload: dict[str, Any], current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    label = str(payload.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="device_label_required")
    ok = WORKSPACE.rename_device(user_id_from_user(current_user), device_id, label)
    if not ok:
        raise HTTPException(status_code=404, detail="device_not_found")
    return {"ok": True}


@router.delete("/api/devices/{device_id}")
def revoke_device(device_id: int, current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    devices = WORKSPACE.list_devices(user_id)
    if len(devices) <= 1:
        raise HTTPException(status_code=409, detail="cannot_remove_only_device")
    if not WORKSPACE.revoke_device(user_id, device_id):
        raise HTTPException(status_code=404, detail="device_not_found")
    WORKSPACE.create_notification(user_id, event_key=f"device-revoked:{device_id}:{time.time_ns()}", kind="security", severity="warning", title="นำอุปกรณ์ออกแล้ว", body="อุปกรณ์ที่เลือกจะไม่สามารถกู้ Guest ผ่าน Browser Key เดิมได้")
    return {"ok": True}


@router.post("/api/account/recovery-code")
def create_recovery_code(current_user: dict = Depends(require_request_user)) -> dict[str, Any]:
    user_id = user_id_from_user(current_user)
    code = WORKSPACE.create_recovery_code(user_id)
    WORKSPACE.create_notification(user_id, event_key=f"recovery-code:{time.time_ns()}", kind="security", title="สร้าง Recovery Code ใหม่แล้ว", body="รหัสเก่าถูกยกเลิก กรุณาเก็บรหัสใหม่ไว้นอก Chrome")
    return {"ok": True, **code, "one_time": True}


@router.post("/api/auth/recover-browser")
def recover_browser(payload: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    user_id = WORKSPACE.consume_recovery_code(str(body.get("code") or ""))
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_or_expired_recovery_code")
    result = BROWSER.bind_existing_user(
        _browser_key(body, request), user_id,
        label=str(body.get("browser_label") or "Recovered Chrome")[:255],
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "recovery_failed"))
    _set_cookie(response, request, str(result.get("browser_key") or ""))
    WORKSPACE.create_notification(user_id, event_key=f"device-recovered:{time.time_ns()}", kind="security", severity="success", title="กู้คืนงานบนอุปกรณ์ใหม่สำเร็จ", body="คิว ประวัติ และไฟล์ของคุณซิงก์มายัง Chrome นี้แล้ว")
    return result
