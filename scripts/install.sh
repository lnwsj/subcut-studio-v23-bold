#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "ไม่พบ $PYTHON_BIN" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ไม่พบ ffmpeg กรุณาติดตั้ง ffmpeg + ffprobe ก่อน" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "ไม่พบ ffprobe" >&2; exit 1; }

"$PYTHON_BIN" -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
python -m pip install --upgrade pip wheel setuptools

if [[ -n "${PYTORCH_INDEX_URL:-}" ]]; then
  python -m pip install torch --index-url "$PYTORCH_INDEX_URL"
fi
python -m pip install -r "$ROOT/app/requirements.txt"

if [[ ! -f "$ROOT/app/.env" ]]; then
  cp "$ROOT/app/.env.example" "$ROOT/app/.env"
fi
python "$ROOT/scripts/prepare_env.py" "$ROOT/app/.env"

echo "ติดตั้งเสร็จแล้ว"
echo "เริ่มระบบ: $ROOT/scripts/start.sh"
echo "เปิดเว็บ: http://127.0.0.1:8787"
