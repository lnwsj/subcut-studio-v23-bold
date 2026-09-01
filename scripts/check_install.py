"""Quick dependency and runtime diagnostic."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys


def command_version(name: str) -> str:
    path = shutil.which(name)
    if not path:
        return "missing"
    result = subprocess.run([path, "-version"], capture_output=True, text=True, check=False)
    return (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else path


modules = ["fastapi", "uvicorn", "multipart", "requests", "numpy", "numba", "whisper", "pythainlp"]
report = {
    "python": sys.version.split()[0],
    "ffmpeg": command_version("ffmpeg"),
    "ffprobe": command_version("ffprobe"),
    "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
    "cuda": {"torch_installed": False, "available": False, "device": ""},
}
try:
    import torch

    report["cuda"]["torch_installed"] = True
    report["cuda"]["available"] = bool(torch.cuda.is_available())
    if torch.cuda.is_available():
        report["cuda"]["device"] = str(torch.cuda.get_device_name(0))
except Exception as exc:  # pragma: no cover - diagnostic only
    report["cuda"]["error"] = str(exc)

print(json.dumps(report, ensure_ascii=False, indent=2))
