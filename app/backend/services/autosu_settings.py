from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import APP_DIR


AUTOSU_SETTINGS_PATH = APP_DIR / "autosu_settings.json"

AUTOSU_RANDOM_TEMPLATE_ID = "random_mix"

AUTOSU_SUBTITLE_TEMPLATE_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "default", "name": "Default", "sample_class": "default", "description": "Clean white text with black outline."},
    {"id": "outline", "name": "Outline", "sample_class": "outline", "description": "Thicker outline for noisy footage."},
    {"id": "yellow_pop", "name": "Yellow Pop", "sample_class": "yellow", "description": "TikTok-style yellow caption."},
    {"id": "neon", "name": "Neon", "sample_class": "neon", "description": "Cyan neon glow."},
    {"id": "blue_glow", "name": "Blue Glow", "sample_class": "blue", "description": "White text with blue glow."},
    {"id": "box", "name": "Box", "sample_class": "box", "description": "Readable boxed caption."},
    {"id": "sticker", "name": "Sticker", "sample_class": "sticker", "description": "White sticker text with pink edge."},
    {"id": "green_pop", "name": "Green Pop", "sample_class": "green", "description": "Green gaming-style caption."},
    {"id": "karaoke_highlight", "name": "Karaoke", "sample_class": "karaoke", "description": "ASS karaoke highlight follows words."},
    {"id": "luxury_gold", "name": "Luxury Gold", "sample_class": "gold", "description": "Premium gold headline look."},
    {"id": "comic_burst", "name": "Comic Burst", "sample_class": "comic", "description": "Big comic style for hooks."},
    {"id": "red_alert", "name": "Red Alert", "sample_class": "red", "description": "Urgent red caption."},
    {"id": "purple_punch", "name": "Purple Punch", "sample_class": "purple", "description": "Purple creator style."},
    {"id": "cyan_ice", "name": "Cyan Ice", "sample_class": "ice", "description": "Cool cyan-white caption."},
    {"id": "orange_sale", "name": "Orange Sale", "sample_class": "orange", "description": "Orange promo caption."},
    {"id": "white_shadow", "name": "White Shadow", "sample_class": "white-shadow", "description": "Minimal white with soft shadow."},
    {"id": "black_label", "name": "Black Label", "sample_class": "black-label", "description": "High-contrast label bar."},
    {"id": "retro_pixel", "name": "Retro Pixel", "sample_class": "pixel", "description": "Retro pixel headline."},
    {"id": "soft_pastel", "name": "Soft Pastel", "sample_class": "pastel", "description": "Soft pastel caption."},
    {"id": "lime_pop", "name": "Lime Pop", "sample_class": "lime", "description": "Bright lime social caption."},
    {"id": AUTOSU_RANDOM_TEMPLATE_ID, "name": "Random Mix", "sample_class": "random", "description": "Random preset plus small typography/color variation per output."},
)

AUTOSU_SUBTITLE_TEMPLATE_IDS = {item["id"] for item in AUTOSU_SUBTITLE_TEMPLATE_CATALOG}
AUTOSU_FIXED_SUBTITLE_TEMPLATE_IDS = {
    item["id"] for item in AUTOSU_SUBTITLE_TEMPLATE_CATALOG if item["id"] != AUTOSU_RANDOM_TEMPLATE_ID
}


def autosu_subtitle_template_catalog() -> list[dict[str, str]]:
    return [dict(item) for item in AUTOSU_SUBTITLE_TEMPLATE_CATALOG]


def _normalize_hex_color(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if raw.startswith("0x"):
        raw = raw[2:]
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        return fallback
    if any(ch not in "0123456789abcdefABCDEF" for ch in raw):
        return fallback
    return f"#{raw.upper()}"


def _normalize_subtitle_template_id(value: Any) -> str:
    template_id = str(value or "default").strip().lower().replace("-", "_")
    return template_id if template_id in AUTOSU_SUBTITLE_TEMPLATE_IDS else "default"


_WINDOWS_FONT_CANDIDATES = (
    r"H:\My Drive\00 mv\20260425\โปรแกรมตัดต่อ Full\BaiJamjuree-Regular.ttf",
    r"C:\Windows\Fonts\THSarabunNew.ttf",
    r"C:\Windows\Fonts\Tahoma.ttf",
    r"C:\Windows\Fonts\LeelawUI.ttf",
    r"C:\Windows\Fonts\Arial.ttf",
)

_LINUX_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThaiUI-Regular.ttf",
    "/usr/share/fonts/truetype/thai-tlwg/Garuda.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _host_is_windows() -> bool:
    return os.name == "nt"


def _default_font_candidates() -> tuple[str, ...]:
    return _WINDOWS_FONT_CANDIDATES if _host_is_windows() else _LINUX_FONT_CANDIDATES


def _looks_like_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", str(value or "").strip()))


def default_font_path_for_host() -> str:
    for raw_path in _default_font_candidates():
        try:
            path = Path(raw_path).expanduser()
            if path.exists() and path.is_file():
                return str(path)
        except Exception:
            continue
    candidates = _default_font_candidates()
    return candidates[0] if candidates else ""


def _normalize_font_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return default_font_path_for_host()
    if not _host_is_windows():
        # Server is Linux: reject Windows drive-letter path from old config.
        if _looks_like_windows_path(raw):
            return default_font_path_for_host()
        if "\\" in raw and "/" not in raw:
            raw = raw.replace("\\", "/")
    return raw


def autosu_runtime_info() -> dict[str, str]:
    return {
        "server_os": "windows" if _host_is_windows() else "linux",
        "font_path_suggestion": default_font_path_for_host(),
    }


@dataclass(slots=True)
class AutoSuSettings:
    enabled: bool = True
    model_name: str = "medium"
    subtitle_template_id: str = "default"
    language: str = "th"
    position: str = "bottom"
    position_percent: int = 88
    font_size: int = 42
    font_color: str = "#FFFFFF"
    border_color: str = "#000000"
    background_color: str = "#000000"
    background_opacity: int = 20
    font_path: str = ""
    max_words_per_line: int = 3
    max_syllables_per_line: int = 1
    speech_check_before_burn: bool = True
    ai_spellfix_before_burn: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoSuSettings":
        if not isinstance(data, dict):
            return cls()
        model_name = str(data.get("model_name") or "medium").strip() or "medium"
        language = str(data.get("language") or "th").strip().lower()
        if language not in {"th", "en", "auto"}:
            language = "th"
        position = str(data.get("position") or "bottom").strip().lower()
        if position not in {"top", "center", "bottom"}:
            position = "bottom"
        raw_position_percent = data.get("position_percent")
        if raw_position_percent is None:
            raw_position_percent = 10 if position == "top" else 50 if position == "center" else 88
        position_percent = max(0, min(100, int(raw_position_percent)))
        if position_percent <= 45:
            position = "top"
        elif position_percent >= 55:
            position = "bottom"
        else:
            position = "center"
        return cls(
            enabled=bool(data.get("enabled", True)),
            model_name=model_name,
            subtitle_template_id=_normalize_subtitle_template_id(data.get("subtitle_template_id")),
            language=language,
            position=position,
            position_percent=position_percent,
            font_size=max(18, min(220, int(data.get("font_size", 42) or 42))),
            font_color=_normalize_hex_color(data.get("font_color"), "#FFFFFF"),
            border_color=_normalize_hex_color(data.get("border_color"), "#000000"),
            background_color=_normalize_hex_color(data.get("background_color"), "#000000"),
            background_opacity=max(0, min(100, int(data.get("background_opacity", 20) or 20))),
            font_path=_normalize_font_path(data.get("font_path")),
            max_words_per_line=max(1, min(20, int(data.get("max_words_per_line", 3) or 3))),
            max_syllables_per_line=max(1, min(40, int(data.get("max_syllables_per_line", 1) or 1))),
            speech_check_before_burn=bool(data.get("speech_check_before_burn", True)),
            ai_spellfix_before_burn=bool(data.get("ai_spellfix_before_burn", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_autosu_settings() -> AutoSuSettings:
    path = AUTOSU_SETTINGS_PATH
    try:
        if not path.exists():
            settings = AutoSuSettings()
            save_autosu_settings(settings)
            return settings
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        settings = AutoSuSettings.from_dict(data)
        raw_font_path = ""
        if isinstance(data, dict):
            raw_font_path = str(data.get("font_path") or "").strip()
        # One-time cleanup/persist for legacy Windows path or empty path on Linux host.
        if not _host_is_windows() and settings.font_path and raw_font_path != settings.font_path:
            save_autosu_settings(settings)
        return settings
    except Exception:
        return AutoSuSettings()


def save_autosu_settings(settings: AutoSuSettings) -> tuple[bool, str]:
    path = AUTOSU_SETTINGS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True, str(path)
    except Exception as exc:
        return False, str(exc)


def autosu_settings_path() -> Path:
    return AUTOSU_SETTINGS_PATH
