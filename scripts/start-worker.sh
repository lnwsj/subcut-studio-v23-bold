#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "ไม่พบ virtualenv กรุณารัน scripts/install.sh ก่อน" >&2
  exit 1
fi
cd "$ROOT"
exec "$PYTHON" app/worker_main.py "$@"
