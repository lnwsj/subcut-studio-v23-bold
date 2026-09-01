"""Authentication routes including automatic browser-bound guest accounts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...config import DB_PATH
from ...services.auth_service import AuthService
from ...services.browser_auth_service import BrowserAuthService

router = APIRouter(prefix="", tags=["Auth"])
_auth = AuthService(DB_PATH)
_browser = BrowserAuthService(DB_PATH, _auth)


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value


def _browser_key(payload: dict[str, Any], request: Request) -> str:
    supplied = payload.get("browser_key") or payload.get("device_token")
    return _browser.normalize_key(supplied or request.cookies.get(_browser.cookie_name, ""))


def _current_user_id(request: Request) -> int:
    user = _auth.resolve_user_from_request(request)
    return int(user.get("id") or 0) if user else 0


def _set_browser_cookie(response: Response, request: Request, key: str) -> None:
    if not key:
        return
    response.set_cookie(
        key=_browser.cookie_name,
        value=key,
        max_age=_browser.cookie_max_age,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def _raise_auth_error(result: dict[str, Any], *, default: str) -> None:
    error = str(result.get("error") or default)
    if error in {"account_pending", "account_rejected", "account_disabled"}:
        raise HTTPException(status_code=403, detail=error)
    if "already" in error.lower():
        raise HTTPException(status_code=409, detail=error)
    if error in {"Invalid email or password"}:
        raise HTTPException(status_code=401, detail=error)
    raise HTTPException(status_code=400, detail=error)


@router.post("/api/auth/browser")
def browser_session(
    request: Request,
    response: Response,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = payload or {}
    key = _browser_key(body, request)
    result = _browser.ensure_session(
        key,
        label=str(body.get("browser_label") or "")[:255],
        force_new=bool(body.get("force_new")),
    )
    if not result.get("ok"):
        _raise_auth_error(result, default="browser_session_failed")
    _set_browser_cookie(response, request, str(result.get("browser_key") or ""))
    return result


@router.post("/api/auth/register")
def register(payload: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    email = _require_text(payload, "email")
    password = _require_text(payload, "password")
    display_name = str(payload.get("display_name") or payload.get("name") or "").strip()
    key = _browser_key(payload, request)
    current_id = _current_user_id(request)
    current = _auth._get_user_by_id(current_id) if current_id else None

    if _browser.is_guest(current) or (key and not current):
        result = _browser.claim_guest(
            browser_key=key,
            current_user_id=current_id,
            email=email,
            password=password,
            display_name=display_name,
        )
    else:
        result = _auth.register(email=email, password=password, display_name=display_name)
        if result.get("access_token") and key:
            _auth.logout(str(result.get("refresh_token") or ""))
            result = _browser.link_authenticated_user(
                browser_key=key,
                current_user_id=current_id,
                target_user_id=int(result["user"]["id"]),
            )
    if not result.get("ok"):
        _raise_auth_error(result, default="register_failed")
    _set_browser_cookie(response, request, str(result.get("browser_key") or key))
    return result


@router.post("/api/auth/login")
def login(payload: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    email = _require_text(payload, "email")
    password = _require_text(payload, "password")
    key = _browser_key(payload, request)
    current_id = _current_user_id(request)
    result = (
        _browser.login_and_link(
            browser_key=key,
            current_user_id=current_id,
            email=email,
            password=password,
        )
        if key
        else _auth.login(email=email, password=password)
    )
    if not result.get("ok"):
        _raise_auth_error(result, default="login_failed")
    _set_browser_cookie(response, request, str(result.get("browser_key") or key))
    return result


@router.get("/api/auth/me")
def me(request: Request) -> dict[str, Any]:
    user = _auth.resolve_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return _auth.public_user(user)


@router.post("/api/auth/refresh")
def refresh(payload: dict[str, Any]) -> dict[str, Any]:
    result = _auth.refresh(_require_text(payload, "refresh_token"))
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=str(result.get("error") or "refresh_failed"))
    return result


@router.post("/api/auth/logout")
def logout(payload: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(payload.get("refresh_token") or "").strip()
    return _auth.logout(refresh_token) if refresh_token else {"ok": True}


@router.post("/api/auth/sso/exchange")
def exchange_sso(payload: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    ticket = _require_text(payload, "ticket")
    current_id = _current_user_id(request)
    result = _auth.exchange_class_sso_ticket(ticket)
    if not result.get("ok"):
        error = str(result.get("error") or "sso_exchange_failed")
        status = 400 if error == "invalid_ticket_format" else 502 if error == "class_sso_unreachable" else 401
        if error == "email_already_registered":
            status = 409
        raise HTTPException(status_code=status, detail=error)

    key = _browser_key(payload, request)
    if key:
        _auth.logout(str(result.get("refresh_token") or ""))
        result = _browser.link_authenticated_user(
            browser_key=key,
            current_user_id=current_id,
            target_user_id=int(result["user"]["id"]),
        )
        _set_browser_cookie(response, request, key)
    return result
