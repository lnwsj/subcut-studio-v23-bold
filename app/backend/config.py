"""Environment configuration for the standalone SJ88 SubCut Studio."""

from __future__ import annotations

import os
import warnings
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


_load_env_file(APP_DIR / ".env")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, str(default)).strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = APP_DIR / candidate
    return candidate.resolve()


DATA_DIR = _path("APP_DATA_DIR", APP_DIR / "data")
STATIC_DIR = APP_DIR / "frontend" / "static"
USER_WORKSPACE_ROOT = _path("APP_USER_WORKSPACE_ROOT", DATA_DIR / "user_workspaces")
LEGACY_UI_JOBS_DIR = _path("APP_LEGACY_UI_JOBS_DIR", DATA_DIR / "legacy_ui_jobs")
DOWNLOAD_CACHE_DIR = _path("APP_DOWNLOAD_CACHE_DIR", DATA_DIR / "download_cache")

DB_ENGINE = os.getenv("APP_DB_ENGINE", "sqlite").strip().lower()
if DB_ENGINE not in {"sqlite", "mysql"}:
    raise RuntimeError("APP_DB_ENGINE must be sqlite or mysql")
DB_PATH = _path("APP_DB_PATH", DATA_DIR / "jobs.db")
DB_HOST = os.getenv("APP_DB_HOST", "127.0.0.1").strip()
DB_PORT = _integer("APP_DB_PORT", 3306, 1, 65535)
DB_USER = os.getenv("APP_DB_USER", "").strip()
DB_PASSWORD = os.getenv("APP_DB_PASSWORD", "")
DB_NAME = os.getenv("APP_DB_NAME", "").strip()
DB_CONNECT_TIMEOUT_SEC = _integer("APP_DB_CONNECT_TIMEOUT_SEC", 5, 1, 60)
DB_READ_TIMEOUT_SEC = _integer("APP_DB_READ_TIMEOUT_SEC", 15, 1, 120)
DB_WRITE_TIMEOUT_SEC = _integer("APP_DB_WRITE_TIMEOUT_SEC", 15, 1, 120)
if DB_ENGINE == "mysql" and (not DB_USER or not DB_NAME):
    raise RuntimeError("MySQL mode requires APP_DB_USER and APP_DB_NAME")

AUTH_REQUIRED = _flag("APP_AUTH_REQUIRED", "1")
_AUTH_SECRET_DEFAULT = "replace-this-before-production"
AUTH_SECRET = os.getenv("APP_AUTH_SECRET", _AUTH_SECRET_DEFAULT).strip() or _AUTH_SECRET_DEFAULT
BROWSER_IDENTITY_SECRET = (
    os.getenv("APP_BROWSER_IDENTITY_SECRET", AUTH_SECRET).strip() or AUTH_SECRET
)
if AUTH_REQUIRED and AUTH_SECRET == _AUTH_SECRET_DEFAULT:
    warnings.warn(
        "APP_AUTH_SECRET is using the example value. Set a long random secret before public deployment.",
        stacklevel=1,
    )
AUTH_ACCESS_TTL_SEC = _integer("APP_AUTH_ACCESS_TTL_SEC", 86400, 60, 31_536_000)
AUTH_REFRESH_TTL_SEC = _integer("APP_AUTH_REFRESH_TTL_SEC", 2_592_000, 300, 63_072_000)
CLASS_SSO_EXCHANGE_URL = os.getenv("APP_CLASS_SSO_EXCHANGE_URL", "").strip()
CLASS_SSO_SHARED_SECRET = os.getenv("APP_CLASS_SSO_SHARED_SECRET", "").strip()
CLASS_SSO_TIMEOUT_SEC = _number("APP_CLASS_SSO_TIMEOUT_SEC", 8.0, 2.0, 60.0)

_cors = os.getenv("CORS_ORIGINS", "http://localhost:8787,http://127.0.0.1:8787").strip()
CORS_ORIGINS = [item.strip() for item in _cors.split(",") if item.strip()]
if _cors == "*":
    CORS_ORIGINS = ["*"]

ENABLE_EMBEDDED_WORKER = _flag("APP_ENABLE_WORKER", "1")
WORKER_POLL_INTERVAL = _number("APP_WORKER_POLL_INTERVAL", 1.0, 0.2, 60.0)
WORKER_MAX_CONCURRENCY = _integer("APP_WORKER_MAX_CONCURRENCY", 1, 1, 8)
JOB_LEASE_TTL_SEC = _integer("APP_JOB_LEASE_TTL_SEC", 90, 30, 3600)
JOB_HEARTBEAT_INTERVAL_SEC = _integer(
    "APP_JOB_HEARTBEAT_INTERVAL_SEC", 10, 2, max(2, JOB_LEASE_TTL_SEC // 3)
)
JOB_QUEUE_SOFT_LIMIT = _integer("APP_JOB_QUEUE_SOFT_LIMIT", 0, 0, 100_000)
FORCED_VIDEO_ENCODER = (
    os.getenv("APP_FORCE_VIDEO_ENCODER", "").strip()
    or os.getenv("SMART_AUTOCUT_ENCODER", "").strip()
)

CHUNK_SIZE = _integer("APP_UPLOAD_CHUNK_SIZE_BYTES", 64 * 1024 * 1024, 1024 * 1024, 512 * 1024 * 1024)
DOWNLOAD_CACHE_RETENTION_HOURS = _number("APP_DOWNLOAD_CACHE_RETENTION_HOURS", 24.0, 0.0, 8760.0)
DOWNLOAD_CACHE_MAX_BYTES = _integer(
    "APP_DOWNLOAD_CACHE_MAX_BYTES", 4 * 1024 * 1024 * 1024, 0, 8 * 1024 * 1024 * 1024 * 1024
)
DOWNLOAD_CACHE_MIN_FREE_BYTES = _integer(
    "APP_DOWNLOAD_CACHE_MIN_FREE_BYTES", 3 * 1024 * 1024 * 1024, 0, 8 * 1024 * 1024 * 1024 * 1024
)
STORAGE_QUOTA_BYTES = _integer(
    "APP_STORAGE_QUOTA_BYTES", 20 * 1024 * 1024 * 1024, 1024 * 1024 * 1024, 8 * 1024 * 1024 * 1024 * 1024
)
OUTPUT_RETENTION_DAYS = _integer("APP_OUTPUT_RETENTION_DAYS", 7, 1, 3650)
OUTPUT_RETENTION_EXTENSION_MAX_DAYS = _integer("APP_OUTPUT_RETENTION_EXTENSION_MAX_DAYS", 365, 1, 3650)

# Compatibility constants retained for imported helper/service code.
ALLOW_UNSAFE_PATHS = False
