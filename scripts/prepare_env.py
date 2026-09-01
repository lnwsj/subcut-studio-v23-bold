"""Replace example secrets in an environment file without overwriting real values."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

PLACEHOLDERS = {
    "replace-with-at-least-32-random-characters": secrets.token_urlsafe(48),
    "replace-with-another-stable-random-secret": secrets.token_urlsafe(48),
}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "app/.env")
    text = path.read_text(encoding="utf-8")
    for old, new in PLACEHOLDERS.items():
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
