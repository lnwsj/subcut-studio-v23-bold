"""Environment-backed Whisper model slot registry.

The registry intentionally separates model selection from per-user AutoSu
settings.  Only allowlisted slots can become active, and the env file is read
for each operation so the worker sees an admin switch before the next job.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Mapping

from ..config import APP_DIR


MODEL_SLOT_PATTERN = re.compile(r"^Model_[0-9]{3}$")
SUPPORTED_BACKENDS = {"openai_whisper", "hf_transformers", "faster_whisper"}
DEFAULT_ACTIVE_SLOT = "Model_001"
DEFAULT_ENV_FILE = APP_DIR.parent / "env" / "whisper-models.env"
_WRITE_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class WhisperModelSlot:
    slot_id: str
    label: str
    backend: str
    source: str
    sha256: str
    repo: str
    revision: str
    base_model: str
    device: str
    dtype: str
    strict_gpu: bool = False

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            [
                self.slot_id,
                self.backend,
                self.source,
                self.sha256,
                self.repo,
                self.revision,
                self.device,
                self.dtype,
                "strict_gpu" if self.strict_gpu else "standard_gpu",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fingerprint"] = self.fingerprint
        return payload


def _env_file_path() -> Path:
    configured = str(os.getenv("APP_WHISPER_MODEL_ENV_FILE", "")).strip()
    return Path(configured).expanduser() if configured else DEFAULT_ENV_FILE


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def _registry_values() -> dict[str, str]:
    values = {key: str(value) for key, value in os.environ.items()}
    # The file must win over process env so an atomic admin switch is visible
    # to an already-running worker before it claims the next job.
    values.update(_parse_env_file(_env_file_path()))
    return values


def _bool_value(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = str(values.get(key, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _slot_ids(values: Mapping[str, str]) -> list[str]:
    configured = {
        f"Model_{match.group(1)}"
        for key in values
        if (match := re.fullmatch(r"APP_WHISPER_MODEL_([0-9]{3})_BACKEND", key))
    }
    configured.add(DEFAULT_ACTIVE_SLOT)
    return sorted(configured)


def _slot_from_values(slot_id: str, values: Mapping[str, str]) -> WhisperModelSlot:
    if not MODEL_SLOT_PATTERN.fullmatch(slot_id):
        raise ValueError(f"invalid Whisper model slot: {slot_id}")
    slot_number = slot_id.removeprefix("Model_")
    prefix = f"APP_WHISPER_MODEL_{slot_number}_"
    defaults = {
        "LABEL": "Current Production Whisper Large" if slot_id == DEFAULT_ACTIVE_SLOT else slot_id,
        "BACKEND": "openai_whisper" if slot_id == DEFAULT_ACTIVE_SLOT else "",
        "SOURCE": "large" if slot_id == DEFAULT_ACTIVE_SLOT else "",
        "SHA256": "",
        "REPO": "",
        "REVISION": "",
        "BASE_MODEL": "",
        "DEVICE": "cuda",
        "DTYPE": "float16",
    }
    field_values = {
        name.lower(): str(values.get(prefix + name, default)).strip()
        for name, default in defaults.items()
    }
    return WhisperModelSlot(
        slot_id=slot_id,
        **field_values,
        strict_gpu=_bool_value(values, prefix + "STRICT_GPU", False),
    )


def list_slots() -> list[WhisperModelSlot]:
    values = _registry_values()
    return [_slot_from_values(slot_id, values) for slot_id in _slot_ids(values)]


def get_slot(slot_id: str) -> WhisperModelSlot:
    values = _registry_values()
    allowed = _slot_ids(values)
    if slot_id not in allowed:
        raise KeyError(f"Whisper model slot is not configured: {slot_id}")
    return _slot_from_values(slot_id, values)


def get_active_slot() -> WhisperModelSlot:
    values = _registry_values()
    slot_id = str(values.get("APP_WHISPER_ACTIVE_MODEL", DEFAULT_ACTIVE_SLOT)).strip()
    if slot_id not in _slot_ids(values):
        raise RuntimeError(f"active Whisper model slot is not configured: {slot_id}")
    return _slot_from_values(slot_id, values)


def require_cuda() -> bool:
    return _bool_value(_registry_values(), "APP_WHISPER_REQUIRE_CUDA", True)


def allow_remote_download() -> bool:
    return _bool_value(_registry_values(), "APP_WHISPER_ALLOW_REMOTE_DOWNLOAD", False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    ignored = {"model-slot-manifest.json", "model-slot-manifest.sha256"}
    files = sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and item.name not in ignored
    )
    for item in files:
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_slot(slot_id: str, *, verify_hash: bool = False, check_cuda: bool = False) -> dict[str, Any]:
    try:
        slot = get_slot(slot_id)
    except (KeyError, ValueError) as exc:
        return {"ok": False, "slot_id": slot_id, "errors": [str(exc)], "warnings": []}

    errors: list[str] = []
    warnings: list[str] = []
    backend = slot.backend.lower()
    if backend not in SUPPORTED_BACKENDS:
        errors.append(f"unsupported backend: {slot.backend or '<empty>'}")
    if slot.device.lower() != "cuda" and require_cuda():
        errors.append("CUDA is required; slot device must be cuda")
    if slot.strict_gpu and slot.device.lower() != "cuda":
        errors.append("strict GPU mode requires device=cuda")
    if slot.strict_gpu and slot.dtype.lower() not in {"float16", "bfloat16"}:
        errors.append("strict GPU mode requires dtype=float16 or bfloat16")

    source = Path(slot.source).expanduser() if slot.source else None
    source_exists = bool(source and source.exists())
    if backend == "openai_whisper":
        if not slot.source:
            errors.append("OpenAI Whisper source is empty")
        elif not source_exists and slot.source not in {"tiny", "base", "small", "medium", "large", "large-v3"}:
            errors.append(f"OpenAI Whisper checkpoint does not exist: {slot.source}")
        elif not source_exists and not allow_remote_download():
            errors.append(
                "remote download is disabled and the configured OpenAI Whisper source is not a local checkpoint"
            )
        if source_exists and source and not source.is_file():
            errors.append(f"OpenAI Whisper source is not a file: {slot.source}")

    elif backend == "faster_whisper":
        if not slot.source:
            errors.append("faster-whisper source is empty")
        # accept either an HF repo id (contains '/') or a local directory
        if slot.source and "/" not in slot.source and not Path(slot.source).expanduser().exists():
            errors.append(
                f"faster-whisper source looks like a bare model name: {slot.source} "
                f"(should be an HF repo id like Systran/faster-whisper-large-v3 or a local directory)"
            )
        if slot.source and Path(slot.source).expanduser().exists() and not Path(slot.source).expanduser().is_dir():
            errors.append(f"faster-whisper source is not a directory: {slot.source}")
        # compute_type mapping done in _load_faster_whisper; allow int8/float16/bfloat16
        if slot.dtype.lower() not in {"float16", "int8", "int8_float16", "int8_float32", "bfloat16"}:
            errors.append(
                f"faster-whisper dtype must be one of float16/int8/int8_float16/int8_float32/bfloat16; got {slot.dtype}"
            )
    elif backend == "hf_transformers":
        if not re.fullmatch(r"[0-9a-fA-F]{40}", slot.revision):
            errors.append("Hugging Face revision must be a 40-character immutable commit SHA")
        if not slot.repo:
            errors.append("Hugging Face repo id is empty")
        for module_name in ("transformers", "accelerate", "safetensors"):
            if importlib.util.find_spec(module_name) is None:
                errors.append(f"required Model_002 dependency is missing: {module_name}")
        if not slot.source:
            errors.append("Hugging Face local model source is empty")
        elif not source_exists or not source or not source.is_dir():
            errors.append(f"Hugging Face model directory does not exist: {slot.source}")
        else:
            if not (source / "config.json").is_file():
                errors.append("Hugging Face model config.json is missing")
            weight_files = list(source.glob("*.safetensors")) + list(source.glob("*.bin"))
            if not weight_files and not (source / "model.safetensors.index.json").is_file():
                errors.append("Hugging Face model weights are missing")

    actual_sha256 = ""
    if verify_hash and source_exists and source:
        actual_sha256 = _sha256_directory(source) if source.is_dir() else _sha256_file(source)
        if not slot.sha256:
            errors.append("model SHA256 is not pinned")
        elif actual_sha256.lower() != slot.sha256.lower():
            errors.append("model SHA256 does not match configured value")

    cuda: dict[str, Any] = {"checked": False}
    if check_cuda and not errors:
        cuda = {
            "checked": True,
            "available": False,
            "device_name": "",
            "device_capability": None,
            "bf16_supported": False,
            "error": "",
        }
        try:
            script = (
                "import json,torch; available=bool(torch.cuda.is_available()); "
                "print(json.dumps({'available':available,'device_name':"
                "str(torch.cuda.get_device_name(0)) if available else '',"
                "'device_capability':list(torch.cuda.get_device_capability(0)) if available else None,"
                "'bf16_supported':bool(torch.cuda.is_bf16_supported()) if available else False}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "CUDA probe failed").strip())
            payload = json.loads(result.stdout)
            cuda["available"] = bool(payload.get("available"))
            cuda["device_name"] = str(payload.get("device_name") or "")
            cuda["device_capability"] = payload.get("device_capability")
            cuda["bf16_supported"] = bool(payload.get("bf16_supported"))
            if not cuda["available"] and require_cuda():
                errors.append("CUDA is not available")
            if slot.strict_gpu and slot.dtype.lower() == "bfloat16" and not cuda["bf16_supported"]:
                errors.append("strict GPU bfloat16 is not supported by the active CUDA device")
        except Exception as exc:
            cuda["error"] = str(exc)
            if require_cuda():
                errors.append(f"CUDA validation failed: {exc}")

    return {
        "ok": not errors,
        "slot": slot.public_dict(),
        "installed": source_exists,
        "source_type": "directory" if source_exists and source and source.is_dir() else "file" if source_exists else "missing",
        "actual_sha256": actual_sha256,
        "errors": errors,
        "warnings": warnings,
        "cuda": cuda,
    }


def registry_status(*, verify_hash: bool = False, check_cuda: bool = False) -> dict[str, Any]:
    active = get_active_slot()
    slots = [
        validate_slot(
            slot.slot_id,
            verify_hash=verify_hash,
            check_cuda=check_cuda and slot.slot_id == active.slot_id,
        )
        for slot in list_slots()
    ]
    return {
        "active_slot": active.slot_id,
        "active_fingerprint": active.fingerprint,
        "env_file": str(_env_file_path()),
        "require_cuda": require_cuda(),
        "allow_remote_download": allow_remote_download(),
        "slots": slots,
    }


def _render_env_with_active_slot(original: str, slot_id: str) -> str:
    key = "APP_WHISPER_ACTIVE_MODEL"
    rows = original.splitlines()
    output: list[str] = []
    replaced = False
    for row in rows:
        if row.strip().startswith(f"{key}="):
            if not replaced:
                output.append(f"{key}={slot_id}")
                replaced = True
            continue
        output.append(row)
    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{key}={slot_id}")
    return "\n".join(output).rstrip() + "\n"


def _append_audit(event: Mapping[str, Any]) -> None:
    path = _env_file_path().parent / "whisper-model-audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def activate_slot(
    slot_id: str,
    *,
    actor: str,
    verify_hash: bool = True,
    check_cuda: bool = True,
) -> dict[str, Any]:
    with _WRITE_LOCK:
        validation = validate_slot(slot_id, verify_hash=verify_hash, check_cuda=check_cuda)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        previous = get_active_slot()
        env_path = _env_file_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path: Path | None = None
        if env_path.exists():
            backup_dir = env_path.parent / "backups" / "whisper-model-switch"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{env_path.name}.{timestamp}.bak"
            shutil.copy2(env_path, backup_path)

        rendered = _render_env_with_active_slot(original, slot_id)
        fd, temp_name = tempfile.mkstemp(prefix=f".{env_path.name}.", dir=str(env_path.parent))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, env_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        current = get_active_slot()
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": str(actor or "unknown"),
            "action": "activate",
            "previous_slot": previous.slot_id,
            "active_slot": current.slot_id,
            "active_fingerprint": current.fingerprint,
            "env_backup": str(backup_path) if backup_path else "",
        }
        _append_audit(event)
        return {**event, "validation": validation}


def rollback_to_model_001(*, actor: str) -> dict[str, Any]:
    return activate_slot(DEFAULT_ACTIVE_SLOT, actor=actor, verify_hash=True, check_cuda=True)


def audit_events(limit: int = 100) -> list[dict[str, Any]]:
    path = _env_file_path().parent / "whisper-model-audit.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events[-max(1, min(1000, int(limit))) :][::-1]
