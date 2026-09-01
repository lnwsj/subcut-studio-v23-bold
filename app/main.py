from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _to_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent
    core_dir = repo_root / "core"
    if str(core_dir) not in sys.path:
        sys.path.insert(0, str(core_dir))
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    host = os.getenv("APP_PYTHON_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PYTHON_WEB_PORT", "8787"))
    reload_mode = _to_bool(os.getenv("APP_PYTHON_WEB_RELOAD"))
    uvicorn.run("backend.subcut_main:app", host=host, port=port, reload=reload_mode)
