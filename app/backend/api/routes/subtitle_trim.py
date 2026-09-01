"""Subtitle Trim Silence settings routes (per-user DB).

GET /api/subtitle/trim-settings  — load current user's settings
PUT /api/subtitle/trim-settings  — save current user's settings
GET /api/subtitle/trim-settings/presets — preset catalog

The FFmpeg silence-trim engine is active for both AutoSu combined jobs and
standalone silence-cut jobs. Settings are stored per user and may be overridden
per job.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ._auth_guard import require_request_user
from ...services.subtitle_trim_settings import (
    SUBTITLE_TRIM_SETTINGS_PROFILE,
    SubtitleTrimSettingsStore,
    normalize_subtitle_trim_settings,
    subtitle_trim_presets_catalog,
)


router = APIRouter(prefix="/api/subtitle", tags=["Subtitle"])


_store: SubtitleTrimSettingsStore | None = None


def _get_store() -> SubtitleTrimSettingsStore:
    """Lazy-init the store using the shared DB target from backend.config."""
    global _store
    if _store is None:
        from ...config import DB_PATH
        _store = SubtitleTrimSettingsStore(DB_PATH)
    return _store


def _user_id_from_user(current_user: dict) -> int:
    try:
        return int(current_user.get("id") or current_user.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0


@router.get("/trim-settings")
def get_subtitle_trim_settings(current_user: dict = Depends(require_request_user)) -> dict:
    user_id = _user_id_from_user(current_user)
    settings = _get_store().get_settings(user_id)
    return {
        "ok": True,
        "user_id": user_id,
        "profile": SUBTITLE_TRIM_SETTINGS_PROFILE,
        "settings": settings,
        "source": "db_or_default",
        "engine_implemented": True,
    }


@router.put("/trim-settings")
def update_subtitle_trim_settings(
    payload: dict,
    current_user: dict = Depends(require_request_user),
) -> dict:
    user_id = _user_id_from_user(current_user)
    body = payload if isinstance(payload, dict) else {}
    raw_settings = body.get("settings") if isinstance(body.get("settings"), dict) else body
    if not isinstance(raw_settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object")
    try:
        settings = _get_store().save_settings(user_id, raw_settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "user_id": user_id,
        "profile": SUBTITLE_TRIM_SETTINGS_PROFILE,
        "settings": settings,
        "source": "db",
        "engine_implemented": True,
    }


@router.get("/trim-settings/presets")
def get_subtitle_trim_presets() -> dict:
    return {
        "ok": True,
        "presets": subtitle_trim_presets_catalog(),
    }


def merge_user_subtitle_trim_settings(user_id: int, settings: dict | None) -> dict:
    """Merge per-user trim settings into a job settings dict.

    Mirrors `ab_roll_routes._merge_user_normalize_settings`:
    - If request has any trim_silence_* keys → use them (per-job override)
    - Else → fall back to user's stored DB config
    - Always normalize and tag source/profile
    """
    data = dict(settings or {})
    saved = _get_store().get_settings(user_id)
    request_has_keys = any(
        key.startswith("trim_silence_") or key == "trim_silence"
        for key in data.keys()
    )
    if request_has_keys:
        merged = {**saved, **{k: v for k, v in data.items() if k.startswith("trim_silence_") or k == "trim_silence"}}
        source = "request"
    else:
        merged = saved
        source = "user_db"
    normalized = normalize_subtitle_trim_settings(merged)
    for key, value in normalized.items():
        data[key] = value
    data["subtitle_trim_settings_profile"] = SUBTITLE_TRIM_SETTINGS_PROFILE
    data["subtitle_trim_settings_source"] = source
    return data
