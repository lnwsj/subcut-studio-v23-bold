"""DB-backed per-user Subtitle Trim Silence settings.

This stores the user's preferred silence-trim parameters for the AutoSu
subtitle flow. The engine is implemented in
`autosu_runner._trim_silence_proxy` (mirror of Tk
`ab_roll_processor._trim_a_silence_proxy`) and the worker wires
`build_runtime_subtitle_trim_settings(user_id, job_settings)` to merge
per-job overrides + per-user DB defaults.

Pattern source: services/abroll_normalize_settings.py
Schema: one row per user_id, settings_json blob.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from .db import _connect, _utc_now, is_mysql_backend


SUBTITLE_TRIM_SETTINGS_PROFILE = "subtitle_trim_silence_v1"

SUBTITLE_TRIM_DEFAULTS: dict[str, Any] = {
    "trim_silence": False,            # default OFF — opt-in only
    "trim_silence_threshold_db": -40.0,
    "trim_silence_min_silence_sec": 0.5,
    "trim_silence_margin_sec": 0.0,
    "trim_silence_min_keep_sec": 0.08,
    "trim_silence_min_output_sec": 1.0,
    "trim_silence_preset": "keep_quiet",  # keep_quiet | aggressive | conservative | custom
}

SUBTITLE_TRIM_KEYS = frozenset(SUBTITLE_TRIM_DEFAULTS.keys())


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _to_float(value: Any, default: float, min_value: float, max_value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(min_value, min(max_value, number))


def _to_str(value: Any, default: str, allowed: set[str]) -> str:
    text = str(value or default).strip().lower()
    return text if text in allowed else default


def normalize_subtitle_trim_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize a settings dict, filling missing keys from defaults."""
    data = {**SUBTITLE_TRIM_DEFAULTS, **(raw or {})}
    return {
        "trim_silence": _to_bool(data.get("trim_silence"), False),
        "trim_silence_threshold_db": _to_float(
            data.get("trim_silence_threshold_db"), -40.0, -90.0, 0.0
        ),
        "trim_silence_min_silence_sec": _to_float(
            data.get("trim_silence_min_silence_sec"), 0.5, 0.05, 30.0
        ),
        "trim_silence_margin_sec": _to_float(
            data.get("trim_silence_margin_sec"), 0.0, 0.0, 5.0
        ),
        "trim_silence_min_keep_sec": _to_float(
            data.get("trim_silence_min_keep_sec"), 0.08, 0.0, 5.0
        ),
        "trim_silence_min_output_sec": _to_float(
            data.get("trim_silence_min_output_sec"), 1.0, 0.0, 60.0
        ),
        "trim_silence_preset": _to_str(
            data.get("trim_silence_preset"),
            "keep_quiet",
            {"keep_quiet", "aggressive", "conservative", "custom"},
        ),
    }


def has_subtitle_trim_keys(settings: dict[str, Any] | None) -> bool:
    """Return True if `settings` contains any trim_silence key (per-job override)."""
    if not isinstance(settings, dict):
        return False
    return any(key in settings for key in SUBTITLE_TRIM_KEYS)


class SubtitleTrimSettingsStore:
    """Per-user storage for Subtitle Trim Silence settings (MySQL or SQLite)."""

    def __init__(self, db_path_or_dsn) -> None:
        self._lock = threading.Lock()
        self._db_target = db_path_or_dsn
        self._ensure_schema()

    # ---------- schema ----------
    def _ensure_schema(self) -> None:
        is_mysql = is_mysql_backend()
        with _connect(self._db_target) as conn:
            if is_mysql:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subtitle_trim_settings (
                        user_id BIGINT NOT NULL PRIMARY KEY,
                        settings_json LONGTEXT NOT NULL,
                        created_at VARCHAR(64) NOT NULL,
                        updated_at VARCHAR(64) NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subtitle_trim_settings (
                        user_id INTEGER PRIMARY KEY,
                        settings_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_subtitle_trim_updated "
                    "ON subtitle_trim_settings(updated_at)"
                )
            conn.commit()

    # ---------- CRUD ----------
    @staticmethod
    def _normalize_user_id(user_id: Any) -> int:
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return 0

    def get_settings(self, user_id: Any) -> dict[str, Any]:
        owner_id = self._normalize_user_id(user_id)
        if owner_id <= 0:
            return normalize_subtitle_trim_settings()
        with self._lock, _connect(self._db_target) as conn:
            row = conn.execute(
                "SELECT settings_json FROM subtitle_trim_settings WHERE user_id = ? LIMIT 1",
                (owner_id,),
            ).fetchone()
        if not row:
            return normalize_subtitle_trim_settings()
        # pymysql DictCursor returns dict; sqlite3 returns tuple.
        if isinstance(row, dict):
            raw_value = row.get("settings_json") or ""
        else:
            try:
                raw_value = row[0]
            except (IndexError, TypeError):
                raw_value = ""
        try:
            decoded = json.loads(raw_value) if raw_value else {}
        except (TypeError, ValueError):
            decoded = {}
        return normalize_subtitle_trim_settings(decoded if isinstance(decoded, dict) else {})

    def save_settings(self, user_id: Any, settings: dict[str, Any]) -> dict[str, Any]:
        owner_id = self._normalize_user_id(user_id)
        if owner_id <= 0:
            raise ValueError("invalid user_id")
        normalized = normalize_subtitle_trim_settings(settings)
        payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        now = _utc_now()
        with self._lock, _connect(self._db_target) as conn:
            existing = conn.execute(
                "SELECT 1 FROM subtitle_trim_settings WHERE user_id = ? LIMIT 1",
                (owner_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE subtitle_trim_settings "
                    "SET settings_json = ?, updated_at = ? WHERE user_id = ?",
                    (payload, now, owner_id),
                )
            else:
                conn.execute(
                    "INSERT INTO subtitle_trim_settings "
                    "(user_id, settings_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (owner_id, payload, now, now),
                )
            conn.commit()
        return normalized


# ---------- presets catalog (for the settings page) ----------

SUBTITLE_TRIM_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "keep_quiet",
        "name": "Keep quiet audio (-40dB)",
        "description": "แนะนำ — เก็บเสียงเบาจริงไว้ ตัดเฉพาะช่วงเงียบชัดเจน",
        "threshold_db": -40.0,
        "min_silence_sec": 0.5,
    },
    {
        "id": "aggressive",
        "name": "Aggressive (-35dB)",
        "description": "ตัดเสียงเบามากด้วย เหมาะกับวิดีโอ noisy",
        "threshold_db": -35.0,
        "min_silence_sec": 0.4,
    },
    {
        "id": "conservative",
        "name": "Conservative (-45dB)",
        "description": "เก็บเสียงเบาไว้มากที่สุด ตัดเฉพาะช่วงเงียบสนิท",
        "threshold_db": -45.0,
        "min_silence_sec": 0.7,
    },
    {
        "id": "custom",
        "name": "Custom",
        "description": "ตั้งค่าเอง (threshold, min_silence, margin, min_keep, min_output)",
        "threshold_db": -40.0,
        "min_silence_sec": 0.5,
    },
)


def subtitle_trim_presets_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in SUBTITLE_TRIM_PRESETS]

# ============== Engine-side helpers (added 2026-08-19) ==============
class SubtitleTrimRuntime:
    __slots__ = (
        "trim_silence",
        "trim_silence_threshold_db",
        "trim_silence_min_silence_sec",
        "trim_silence_margin_sec",
        "trim_silence_min_keep_sec",
        "trim_silence_min_output_sec",
        "trim_silence_preset",
    )

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot, None))

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
    return default


def build_runtime_subtitle_trim_settings(user_id=None, job_settings=None):
    user_db = {}
    if user_id is not None:
        try:
            from ..config import DB_PATH

            store = SubtitleTrimSettingsStore(DB_PATH)
            user_db = store.get_settings(int(user_id))
        except Exception:
            user_db = {}
    merged = {}
    for key, default in SUBTITLE_TRIM_DEFAULTS.items():
        if isinstance(user_db, dict) and key in user_db and user_db[key] is not None:
            merged[key] = user_db[key]
        else:
            merged[key] = default
    if isinstance(job_settings, dict):
        for key, default in SUBTITLE_TRIM_DEFAULTS.items():
            value = job_settings.get(key)
            if value is not None:
                merged[key] = value
    normalized = normalize_subtitle_trim_settings(merged)
    return SubtitleTrimRuntime(
        trim_silence=bool(normalized.get("trim_silence", False)),
        trim_silence_threshold_db=float(normalized.get("trim_silence_threshold_db", -40.0)),
        trim_silence_min_silence_sec=float(normalized.get("trim_silence_min_silence_sec", 0.5)),
        trim_silence_margin_sec=float(normalized.get("trim_silence_margin_sec", 0.0)),
        trim_silence_min_keep_sec=float(normalized.get("trim_silence_min_keep_sec", 0.08)),
        trim_silence_min_output_sec=float(normalized.get("trim_silence_min_output_sec", 1.0)),
        trim_silence_preset=str(normalized.get("trim_silence_preset", "keep_quiet")),
    )

