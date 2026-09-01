











from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from ..config import APP_DIR, FORCED_VIDEO_ENCODER
from .subtitle_trim_engine import re_align_cues, trim_silence_proxy
from .autosu_settings import (
    AUTOSU_FIXED_SUBTITLE_TEMPLATE_IDS,
    AUTOSU_RANDOM_TEMPLATE_ID,
    AUTOSU_SUBTITLE_TEMPLATE_IDS,
    AutoSuSettings,
)
from .whisper_backends import load_whisper_backend
from .whisper_model_registry import WhisperModelSlot, get_active_slot, require_cuda, validate_slot

try:
    from app_new_refacter.utils.ffmpeg import get_ffmpeg_path, run_cmd as _run_ffmpeg_cmd
except Exception:  # pragma: no cover
    def get_ffmpeg_path() -> str:  # type: ignore[no-redef]
        return "ffmpeg"

    def _run_ffmpeg_cmd(cmd: list, log_func=None, cwd=None) -> tuple[bool, str]:  # type: ignore[no-redef]
        timeout_sec = float(os.getenv("APP_AUTOSU_FFMPEG_TIMEOUT_SEC", "3600") or 3600)
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            cwd=str(cwd) if cwd else None,
            timeout=timeout_sec,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return result.returncode == 0, str(result.stdout or "")


LogCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]


def _noop(_message: str) -> None:
    return None


def _cancel_false() -> bool:
    return False


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except Exception:
        return float(default)


_ENCODER_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}

# ----------------------------------------------------------------------------
# Whisper model singleton + LRU idle-unload (memory-leak fix 2026-06-11)
# Caches loaded whisper models across requests so we don't re-load 3 GB on
# every transcription. Unloads after N seconds idle to free VRAM.
# ----------------------------------------------------------------------------
_WHISPER_MODEL_CACHE: dict[str, tuple[Any, float]] = {}
_WHISPER_LAST_SWEEP_MONOTONIC: float = 0.0
_WHISPER_LIFECYCLE_LOCK = threading.RLock()


def _whisper_idle_unload_seconds() -> float:
    """Seconds of idle before a cached whisper model is unloaded (env-driven)."""
    return _env_float("APP_WHISPER_IDLE_UNLOAD_SEC", 300.0)


def _whisper_sweep_interval_seconds() -> float:
    """Minimum seconds between sweeps (avoid overhead on every call)."""
    return _env_float("APP_WHISPER_SWEEP_INTERVAL_SEC", 30.0)


def _whisper_cache_max_models() -> int:
    """Hard cap cached Whisper models so model switching cannot stack VRAM."""
    return max(1, min(4, _env_int("APP_WHISPER_CACHE_MAX_MODELS", 1)))


def _whisper_unload_after_run_enabled() -> bool:
    """Unload Whisper cache after each AutoSu run by default to release VRAM."""
    return _env_flag("APP_WHISPER_UNLOAD_AFTER_RUN", "1")


def _whisper_cache_snapshot() -> dict[str, Any]:
    with _WHISPER_LIFECYCLE_LOCK:
        now = time.monotonic()
        return {
            "count": len(_WHISPER_MODEL_CACHE),
            "keys": sorted(_WHISPER_MODEL_CACHE.keys()),
            "max_models": _whisper_cache_max_models(),
            "unload_after_run": _whisper_unload_after_run_enabled(),
            "idle_unload_sec": _whisper_idle_unload_seconds(),
            "ages_sec": {
                key: round(max(0.0, now - last_used), 3)
                for key, (_model, last_used) in _WHISPER_MODEL_CACHE.items()
            },
        }


def _whisper_release_cached_models(keys: list[str], *, log: LogCallback, reason: str) -> int:
    # The same lock is held by the complete transcribe/retry lifecycle.  A
    # release therefore waits until no thread can still be using model CUDA
    # tensors before moving the model to CPU or clearing the allocator cache.
    with _WHISPER_LIFECYCLE_LOCK:
        keys = [key for key in keys if key in _WHISPER_MODEL_CACHE]
        if not keys:
            return 0
        try:
            import torch as _torch  # local import; only needed while unloading
        except Exception as exc:
            _torch = None  # type: ignore[assignment]
            log(f"[AutoSu][WARN] torch import failed during whisper unload: {exc}")

        released = 0
        for key in keys:
            model, _ = _WHISPER_MODEL_CACHE.pop(key)
            try:
                if _torch is not None and hasattr(model, "cpu"):
                    model.cpu()
            except Exception as exc:
                log(f"[AutoSu][WARN] whisper model cpu() failed key={key}: {exc}")
            try:
                del model
            except Exception as exc:
                log(f"[AutoSu][WARN] whisper model delete failed key={key}: {exc}")
            released += 1
            log(f"[AutoSu] whisper model unloaded reason={reason} key={key}")

        try:
            import gc as _gc

            _gc.collect()
        except Exception as exc:
            log(f"[AutoSu][WARN] gc.collect failed after whisper unload: {exc}")
        if _torch is not None:
            try:
                _torch.cuda.empty_cache()
            except Exception as exc:
                log(f"[AutoSu][WARN] torch.cuda.empty_cache failed: {exc}")
            if hasattr(_torch.cuda, "ipc_collect"):
                try:
                    _torch.cuda.ipc_collect()
                except Exception as exc:
                    log(f"[AutoSu][WARN] torch.cuda.ipc_collect failed: {exc}")
        return released


def _whisper_enforce_cache_limit(*, keep_key: str, log: LogCallback) -> int:
    with _WHISPER_LIFECYCLE_LOCK:
        overflow = len(_WHISPER_MODEL_CACHE) - _whisper_cache_max_models()
        if overflow <= 0:
            return 0
        candidates = sorted(
            (last_used, key)
            for key, (_model, last_used) in _WHISPER_MODEL_CACHE.items()
            if key != keep_key
        )
        release_keys = [key for _last_used, key in candidates[:overflow]]
        return _whisper_release_cached_models(release_keys, log=log, reason="cache-limit")


def _whisper_cache_key(slot: WhisperModelSlot) -> str:
    return f"{slot.slot_id}:{slot.fingerprint}@{slot.device}"


def _get_whisper_model_cached(slot: WhisperModelSlot, log: LogCallback) -> tuple[Any, bool]:
    """Return cached whisper model if available, else load + cache.

    Returns (model, was_cached).
    """
    global _WHISPER_LAST_SWEEP_MONOTONIC
    key = _whisper_cache_key(slot)
    # Keep the cache check and the expensive load in one critical section.  If
    # two worker threads miss concurrently, only the first is allowed to load
    # the multi-GB model; the second reuses the newly cached instance.
    with _WHISPER_LIFECYCLE_LOCK:
        now = time.monotonic()
        entry = _WHISPER_MODEL_CACHE.get(key)
        if entry is not None:
            cached_model, _ = entry
            _WHISPER_MODEL_CACHE[key] = (cached_model, now)
            return cached_model, True
        # Sweep before loading a new one to keep cache small.
        if now - _WHISPER_LAST_SWEEP_MONOTONIC >= _whisper_sweep_interval_seconds():
            _whisper_sweep_idle_models(now, log=log)
            _WHISPER_LAST_SWEEP_MONOTONIC = now
        model = load_whisper_backend(
            slot,
            download_root=APP_DIR / "models" / "whisper",
            log=log,
        )
        _WHISPER_MODEL_CACHE[key] = (model, now)
        _whisper_enforce_cache_limit(keep_key=key, log=log)
        return model, False


def _whisper_sweep_idle_models(now: float, *, log: LogCallback) -> None:
    """Unload whisper models that have been idle past the threshold."""
    with _WHISPER_LIFECYCLE_LOCK:
        idle_sec = _whisper_idle_unload_seconds()
        if idle_sec <= 0:
            return  # 0 / negative = disabled (keep forever)
        stale: list[str] = []
        for key, (_model, last_used) in list(_WHISPER_MODEL_CACHE.items()):
            if now - last_used >= idle_sec:
                stale.append(key)
        if not stale:
            return
        _whisper_release_cached_models(stale, log=log, reason="idle")


def _whisper_touch_cached_model(key: str, model: Any, *, now: float | None = None) -> bool:
    """Record when a cached model finished use without reviving a released model."""
    with _WHISPER_LIFECYCLE_LOCK:
        entry = _WHISPER_MODEL_CACHE.get(key)
        if entry is None or entry[0] is not model:
            return False
        _WHISPER_MODEL_CACHE[key] = (model, time.monotonic() if now is None else float(now))
        return True


def _whisper_cleanup_after_transcribe(device: str, *, log: LogCallback) -> None:
    """Free per-request intermediates; caller must hold the lifecycle lock."""
    try:
        import gc as _gc

        _gc.collect()
    except Exception as exc:
        log(f"[AutoSu][WARN] gc.collect failed after whisper transcribe: {exc}")
    if device != "cuda":
        return
    try:
        import torch as _torch
    except Exception as exc:
        log(f"[AutoSu][WARN] torch import failed after whisper transcribe: {exc}")
        return
    try:
        _torch.cuda.empty_cache()
    except Exception as exc:
        log(f"[AutoSu][WARN] torch.cuda.empty_cache failed after whisper transcribe: {exc}")
    try:
        ipc_collect = getattr(_torch.cuda, "ipc_collect", None)
        if callable(ipc_collect):
            ipc_collect()
    except Exception as exc:
        log(f"[AutoSu][WARN] torch.cuda.ipc_collect failed after whisper transcribe: {exc}")


def _prepare_whisper_audio_input(
    input_video: Path,
    *,
    slot: WhisperModelSlot,
    log: LogCallback,
) -> tuple[Path, Any | None, dict[str, Any]]:
    """Convert media containers to a decoder-safe WAV for HF pipelines."""
    if slot.backend != "hf_transformers":
        return input_video, None, {}

    temp_dir = tempfile.TemporaryDirectory(prefix="sj88-subcut-whisper-")
    wav_path = Path(temp_dir.name) / "input.wav"
    cmd = [
        str(get_ffmpeg_path()),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_video),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    try:
        ok, output = _run_ffmpeg_cmd(cmd, log_func=log)
        wav_bytes = wav_path.stat().st_size if wav_path.exists() else 0
        if not ok or wav_bytes <= 44:
            detail = str(output or "").strip()[-1200:]
            raise RuntimeError(
                "failed to prepare 16 kHz mono WAV for Whisper"
                + (f": {detail}" if detail else "")
            )
    except Exception:
        temp_dir.cleanup()
        raise

    log(
        "[AutoSu] prepared decoder-safe Whisper input "
        f"source={input_video.name} format=wav sample_rate=16000 channels=1 bytes={wav_bytes}"
    )
    return (
        wav_path,
        temp_dir,
        {
            "whisper_input_preprocessed": True,
            "whisper_input_source_suffix": input_video.suffix.lower(),
            "whisper_input_audio_codec": "pcm_s16le",
            "whisper_input_sample_rate": 16000,
            "whisper_input_channels": 1,
            "whisper_input_wav_bytes": wav_bytes,
        },
    )


def _transcribe_with_cached_whisper(
    input_video: Path,
    *,
    slot: WhisperModelSlot,
    language: str,
    log: LogCallback,
) -> dict[str, Any]:
    """Run transcription; strict GPU slots fail closed without a retry path."""
    key = _whisper_cache_key(slot)
    prepared_input, prepared_temp_dir, input_telemetry = _prepare_whisper_audio_input(
        input_video,
        slot=slot,
        log=log,
    )
    try:
        with _WHISPER_LIFECYCLE_LOCK:
            model, _was_cached = _get_whisper_model_cached(slot, log)
            try:
                transcribe_kwargs: dict[str, Any] = {
                    # Pathumma Model_002 stays on CUDA, but Transformers' token-level
                    # alignment needs more VRAM than the 6 GB RTX 3050 has. Segment
                    # timestamps keep the GPU-only policy while letting the Thai cue
                    # splitter preserve the configured syllable grouping.
                    "word_timestamps": not (slot.slot_id == "Model_002" and slot.strict_gpu),
                    "beam_size": 1,
                    "best_of": 1,
                    "condition_on_previous_text": False,
                    "verbose": False,
                    "fp16": True,  # device=cuda always (forced GPU)
                }
                if language != "auto":
                    transcribe_kwargs["language"] = language
                try:
                    result = model.transcribe(str(prepared_input), **transcribe_kwargs)
                    runtime = result.setdefault("_whisper_runtime", {})
                    if isinstance(runtime, dict):
                        runtime.update(input_telemetry)
                        runtime["whisper_word_timestamps"] = bool(
                            transcribe_kwargs.get("word_timestamps", True)
                        )
                        runtime["whisper_alignment_mode"] = (
                            "word"
                            if runtime["whisper_word_timestamps"]
                            else "segment"
                        )
                        if not runtime["whisper_word_timestamps"]:
                            runtime["whisper_alignment_reason"] = "model002_cuda_vram_guard"
                        runtime["whisper_alignment_retry_used"] = False
                    return result
                except Exception as exc:
                    if slot.strict_gpu:
                        log(
                            "[AutoSu][GPU_ONLY_FAIL_CLOSED] "
                            f"{input_video.name}: no CPU/model/alignment fallback; "
                            f"slot={slot.slot_id}, backend={slot.backend}, device={slot.device}, error={exc}"
                        )
                        raise RuntimeError(
                            f"Model_002 strict GPU transcription failed without fallback: {exc}"
                        ) from exc
                    if not _is_whisper_alignment_error(exc):
                        raise
                    log(
                        "[AutoSu][ALIGNMENT_RETRY] "
                        f"{input_video.name}: word_timestamps=True failed; "
                        "retrying same file with word_timestamps=False, "
                        f"slot={slot.slot_id}, backend={slot.backend}, device={slot.device}"
                    )
                    retry_kwargs = dict(transcribe_kwargs)
                    retry_kwargs["word_timestamps"] = False
                    result = model.transcribe(str(prepared_input), **retry_kwargs)
                    runtime = result.setdefault("_whisper_runtime", {})
                    if isinstance(runtime, dict):
                        runtime.update(input_telemetry)
                        runtime["whisper_word_timestamps"] = False
                        runtime["whisper_alignment_retry_used"] = True
                    return result
            finally:
                _whisper_cleanup_after_transcribe(slot.device, log=log)
                _whisper_touch_cached_model(key, model)
    finally:
        if prepared_temp_dir is not None:
            try:
                prepared_temp_dir.cleanup()
            except Exception as exc:
                log(f"[AutoSu][WARN] temporary Whisper WAV cleanup failed: {exc}")



def _normalize_encoder_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _ffmpeg_supports_encoder(ffmpeg_exe: str, encoder: str) -> bool:
    token = _normalize_encoder_name(encoder)
    if not token:
        return False
    cache_key = (str(ffmpeg_exe), token)
    cached = _ENCODER_SUPPORT_CACHE.get(cache_key)
    if cached is not None:
        return bool(cached)
    try:
        result = subprocess.run(
            [str(ffmpeg_exe), "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(1.0, _env_float("APP_AUTOSU_FFMPEG_PROBE_TIMEOUT_SEC", 30.0)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        text = str(result.stdout or "")
        ok = result.returncode == 0 and token in text
    except Exception:
        ok = False
    _ENCODER_SUPPORT_CACHE[cache_key] = bool(ok)
    return bool(ok)


def _resolve_autosu_encoder_chain(ffmpeg_exe: str) -> tuple[str, list[str]]:
    forced = _normalize_encoder_name(FORCED_VIDEO_ENCODER or os.getenv("APP_FORCE_VIDEO_ENCODER", ""))
    requested = forced or "h264_nvenc"
    chain: list[str] = []
    for candidate in [requested, "libx264"]:
        enc = _normalize_encoder_name(candidate)
        if not enc or enc in chain:
            continue
        if _ffmpeg_supports_encoder(ffmpeg_exe, enc):
            chain.append(enc)
    if not chain:
        chain = ["libx264"]
    return requested, chain


def _build_video_encode_args(encoder: str) -> list[str]:
    enc = _normalize_encoder_name(encoder)
    if enc == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-cq",
            "19",
            "-b:v",
            "0",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
    ]


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.split())


def _sec_to_srt(seconds: float) -> str:
    ms = max(0, int(round(float(seconds) * 1000.0)))
    hh, rem = divmod(ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, msec = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{msec:03d}"


def _sec_to_ass(seconds: float) -> str:
    cs = max(0, int(round(float(seconds) * 100.0)))
    hh, rem = divmod(cs, 360000)
    mm, rem = divmod(rem, 6000)
    ss, csec = divmod(rem, 100)
    return f"{hh:d}:{mm:02d}:{ss:02d}.{csec:02d}"


def _to_ass_color(hex_value: str, alpha_percent: int = 0) -> str:
    raw = str(hex_value or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        raw = "FFFFFF"
    rr = raw[0:2]
    gg = raw[2:4]
    bb = raw[4:6]
    alpha = max(0, min(100, int(alpha_percent)))
    aa = int(round(alpha * 255 / 100))
    return f"&H{aa:02X}{bb}{gg}{rr}&"


def _ffmpeg_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:")


def _ffmpeg_escape_filter_value(text: str) -> str:
    value = str(text or "")
    value = value.replace("\\", "\\\\")
    value = value.replace("'", r"\'")
    value = value.replace(":", r"\:")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    value = value.replace(";", r"\;")
    value = value.replace("%", r"\%")
    return value


def _ass_escape_text(text: str) -> str:
    value = _normalize_text(text)
    value = value.replace("\\", r"\\")
    value = value.replace("{", r"\{")
    value = value.replace("}", r"\}")
    return value


_AUTOSU_ASS_RAW_PREFIX = "__AUTOSU_ASS_RAW__:"


def _is_karaoke_template(settings: AutoSuSettings) -> bool:
    template_id = str(getattr(settings, "subtitle_template_id", "") or "").strip().lower().replace("-", "_")
    return template_id in {"karaoke_highlight", AUTOSU_RANDOM_TEMPLATE_ID}


def _ass_raw_text(value: str) -> str:
    return f"{_AUTOSU_ASS_RAW_PREFIX}{value}"


def _is_ass_raw_text(value: str) -> bool:
    return str(value or "").startswith(_AUTOSU_ASS_RAW_PREFIX)


def _strip_ass_raw_prefix(value: str) -> str:
    return str(value or "")[len(_AUTOSU_ASS_RAW_PREFIX) :]


_AUTOSU_KARAOKE_TAG_RE = re.compile(r"\{\\kf\d+\}")


def _spellfix_visible_text(value: str) -> str:
    raw = _strip_ass_raw_prefix(value) if _is_ass_raw_text(value) else str(value or "")
    return _normalize_text(_AUTOSU_KARAOKE_TAG_RE.sub("", raw))


def _restore_spellfixed_karaoke_text(original: str, corrected: str) -> str:
    corrected_norm = _normalize_text(corrected)
    if not _is_ass_raw_text(original) or not corrected_norm:
        return corrected_norm or _normalize_text(original)

    raw = _strip_ass_raw_prefix(original)
    tags = _AUTOSU_KARAOKE_TAG_RE.findall(raw)
    if not tags:
        return original
    if len(tags) == 1:
        return _ass_raw_text(f"{tags[0]}{_ass_escape_text(corrected_norm)}")

    corrected_tokens = corrected_norm.split()
    if len(corrected_tokens) != len(tags):
        return original
    rebuilt = " ".join(
        f"{tag}{_ass_escape_text(token)}"
        for tag, token in zip(tags, corrected_tokens)
    )
    return _ass_raw_text(rebuilt)


def _karaoke_duration_cs(start: float, end: float, fallback_cs: int = 20) -> int:
    """Karaoke per-syllable highlight duration (centiseconds).

    Slower than the natural speech gap so the highlight is clearly readable.
    Min 40cs (0.4s) per word, max 80cs (0.8s). Last syllable absorbs the rest.
    """
    try:
        duration_cs = int(round(max(0.01, float(end) - float(start)) * 100.0))
    except Exception:
        duration_cs = fallback_cs
    # Slightly slower: 1.15× the natural gap
    duration_cs = int(round(duration_cs * 1.15))
    return max(40, min(80, duration_cs))


def _karaoke_ass_text(parts: list[tuple[float, float, str]], separator: str = "") -> str:
    rendered: list[str] = []
    for idx, (start, end, raw_text) in enumerate(parts):
        text = _ass_escape_text(raw_text)
        if not text:
            continue
        if idx > 0 and separator:
            rendered.append(separator)
        kf_cs = _karaoke_duration_cs(start, end)
        # Pop effect: scale 1.30x in first 30% of kf, ease back to 1.0x at end
        # kfXX=karaoke duration, \t(...,\fscx...\fscy...)=scale animation
        pop_t = max(8, min(25, int(round(kf_cs * 0.30))))
        rest_t = max(0, kf_cs - pop_t)
        if rest_t > 0:
            tag = (r"{\kf" + str(kf_cs)
                   + r"\t(0," + str(pop_t) + r",\fscx130\fscy130\bord0)"
                   + r"\t(" + str(pop_t) + "," + str(kf_cs) + r",\fscx100\fscy100\bord4)}")
        else:
            tag = (r"{\kf" + str(kf_cs)
                   + r"\t(0," + str(pop_t) + r",\fscx130\fscy130\bord0)}")
        rendered.append(tag + text)
    return "".join(rendered)


def _normalize_runner_template_id(value: Any) -> str:
    template_id = str(value or "default").strip().lower().replace("-", "_")
    return template_id if template_id in AUTOSU_SUBTITLE_TEMPLATE_IDS else "default"


def _random_template_seed(input_video: Path | None, settings: AutoSuSettings) -> tuple[int, str]:
    parts = [
        str(getattr(settings, "subtitle_template_id", "") or ""),
        str(time.time_ns()),
    ]
    if input_video is not None:
        try:
            stat = input_video.stat()
            parts.extend([input_video.name, str(stat.st_mtime_ns), str(stat.st_size)])
        except Exception:
            parts.append(str(input_video))
    digest = hashlib.sha256("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:16], 16), digest[:12]


def _subtitle_template_style(settings: AutoSuSettings, input_video: Path | None = None) -> dict[str, Any]:
    requested_template_id = _normalize_runner_template_id(getattr(settings, "subtitle_template_id", "default"))
    base_size = max(18, min(160, int(settings.font_size or 44)))
    presets: dict[str, dict[str, Any]] = {
        "default": {},
        "outline": {
            "outline_size": 4,
            "shadow_size": 2,
            "bold": 1,
        },
        "yellow_pop": {
            "primary": _to_ass_color("#FFE84D", 0),
            "outline": _to_ass_color("#111111", 0),
            "outline_size": 4,
            "shadow_size": 2,
            "bold": 1,
            "font_size": min(160, base_size + 4),
        },
        "neon": {
            "primary": _to_ass_color("#7DF9FF", 0),
            "outline": _to_ass_color("#0057FF", 0),
            "back": _to_ass_color("#050B2E", 12),
            "outline_size": 4,
            "shadow_size": 3,
            "bold": 1,
        },
        "blue_glow": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#1597FF", 0),
            "back": _to_ass_color("#001A3D", 18),
            "outline_size": 5,
            "shadow_size": 3,
            "bold": 1,
        },
        "box": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#111827", 0),
            "back": _to_ass_color("#000000", 82),
            "outline_size": 2,
            "shadow_size": 0,
            "border_style": 3,
            "bold": 1,
        },
        "sticker": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#FF2E8A", 0),
            "back": _to_ass_color("#000000", 0),
            "outline_size": 5,
            "shadow_size": 2,
            "bold": 1,
            "font_size": min(160, base_size + 3),
        },
        "green_pop": {
            "primary": _to_ass_color("#8CFF4D", 0),
            "outline": _to_ass_color("#053B16", 0),
            "back": _to_ass_color("#001B09", 15),
            "outline_size": 4,
            "shadow_size": 2,
            "bold": 1,
        },
        "karaoke_highlight": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#4DE8FF", 0),
            "outline": _to_ass_color("#111111", 0),
            "back": _to_ass_color("#000000", 12),
            "outline_size": 4,
            "shadow_size": 2,
            "bold": 1,
            "font_size": min(160, base_size + 3),
        },
        "luxury_gold": {
            "primary": _to_ass_color("#FFD166", 0),
            "secondary": _to_ass_color("#FFF2A6", 0),
            "outline": _to_ass_color("#3A2500", 0),
            "back": _to_ass_color("#120A00", 10),
            "outline_size": 4,
            "shadow_size": 3,
            "bold": 1,
            "font_size": min(160, base_size + 5),
            "spacing": 1,
        },
        "comic_burst": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#FFE84D", 0),
            "outline": _to_ass_color("#FF3D00", 0),
            "back": _to_ass_color("#000000", 0),
            "outline_size": 6,
            "shadow_size": 2,
            "bold": 1,
            "font_size": min(160, base_size + 8),
            "scale_x": 104,
        },
        "red_alert": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#FFD6D6", 0),
            "outline": _to_ass_color("#E11D48", 0),
            "back": _to_ass_color("#2A0008", 14),
            "outline_size": 5,
            "shadow_size": 2,
            "bold": 1,
        },
        "purple_punch": {
            "primary": _to_ass_color("#F5D0FE", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#7E22CE", 0),
            "back": _to_ass_color("#14001F", 18),
            "outline_size": 5,
            "shadow_size": 3,
            "bold": 1,
            "font_size": min(160, base_size + 3),
        },
        "cyan_ice": {
            "primary": _to_ass_color("#E0FBFF", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#0891B2", 0),
            "back": _to_ass_color("#001018", 14),
            "outline_size": 4,
            "shadow_size": 3,
            "bold": 1,
        },
        "orange_sale": {
            "primary": _to_ass_color("#FFB703", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#7C2D12", 0),
            "back": _to_ass_color("#1F0700", 12),
            "outline_size": 5,
            "shadow_size": 2,
            "bold": 1,
            "font_size": min(160, base_size + 4),
        },
        "white_shadow": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#1F2937", 0),
            "back": _to_ass_color("#000000", 0),
            "outline_size": 2,
            "shadow_size": 4,
            "bold": 1,
        },
        "black_label": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#FDE68A", 0),
            "outline": _to_ass_color("#111827", 0),
            "back": _to_ass_color("#000000", 78),
            "outline_size": 2,
            "shadow_size": 0,
            "border_style": 3,
            "bold": 1,
            "font_size": min(160, base_size + 2),
        },
        "retro_pixel": {
            "primary": _to_ass_color("#FDE047", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#581C87", 0),
            "back": _to_ass_color("#0F0520", 10),
            "outline_size": 5,
            "shadow_size": 1,
            "bold": 1,
            "spacing": 2,
            "scale_x": 98,
        },
        "soft_pastel": {
            "primary": _to_ass_color("#FCE7F3", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#BE185D", 0),
            "back": _to_ass_color("#2A0517", 12),
            "outline_size": 3,
            "shadow_size": 2,
            "bold": 1,
        },
        "lime_pop": {
            "primary": _to_ass_color("#D9F99D", 0),
            "secondary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#365314", 0),
            "back": _to_ass_color("#071400", 12),
            "outline_size": 5,
            "shadow_size": 2,
            "bold": 1,
            "font_size": min(160, base_size + 3),
        },
        "news_ticker": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#FFD700", 0),
            "outline": _to_ass_color("#000000", 0),
            "back": _to_ass_color("#000000", 60),
            "outline_size": 2,
            "shadow_size": 1,
            "border_style": 3,
            "bold": 1,
            "font_size": 52,
            "alignment": 2,
            "margin_v": 200,
        },
        "bold_thunder": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "outline": _to_ass_color("#000000", 0),
            "back": _to_ass_color("#000000", 20),
            "outline_size": 7,
            "shadow_size": 5,
            "border_style": 1,
            "bold": 1,
            "font_size": 150,
            "alignment": 5,
            "margin_v": 0,
            "scale_x": 110,
        },
        "sticker_pop": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#FFE84D", 0),
            "outline": _to_ass_color("#111111", 0),
            "back": _to_ass_color("#000000", 0),
            "outline_size": 8,
            "shadow_size": 4,
            "border_style": 1,
            "bold": 1,
            "font_size": 100,
            "alignment": 2,
        },
        "instagram": {
            "primary": _to_ass_color("#C026D3", 0),
            "secondary": _to_ass_color("#F472B6", 0),
            "outline": _to_ass_color("#FFFFFF", 0),
            "back": _to_ass_color("#000000", 30),
            "outline_size": 4,
            "shadow_size": 3,
            "border_style": 1,
            "bold": 1,
            "font_size": 78,
            "alignment": 2,
        },
        "subtitle_heavy": {
            "primary": _to_ass_color("#FFFFFF", 0),
            "secondary": _to_ass_color("#E0E0E0", 0),
            "outline": _to_ass_color("#000000", 0),
            "back": _to_ass_color("#000000", 10),
            "outline_size": 3,
            "shadow_size": 5,
            "border_style": 1,
            "bold": 1,
            "font_size": 72,
            "alignment": 2,
            "spacing": 3,
        },
    }
    resolved_template_id = requested_template_id
    randomized = requested_template_id == AUTOSU_RANDOM_TEMPLATE_ID
    random_seed = ""
    if randomized:
        seed_int, random_seed = _random_template_seed(input_video, settings)
        rng = random.Random(seed_int)
        pool = sorted(AUTOSU_FIXED_SUBTITLE_TEMPLATE_IDS)
        resolved_template_id = rng.choice(pool) if pool else "default"
    else:
        rng = random.Random(0)

    style = dict(presets.get(resolved_template_id, {}))
    if randomized:
        palettes = [
            ("#FFFFFF", "#000000", "#000000", 8),
            ("#FFE84D", "#111111", "#000000", 10),
            ("#7DF9FF", "#0057FF", "#050B2E", 12),
            ("#FCE7F3", "#BE185D", "#2A0517", 14),
            ("#D9F99D", "#365314", "#071400", 12),
            ("#FFB703", "#7C2D12", "#1F0700", 12),
        ]
        if rng.random() < 0.45:
            primary_hex, outline_hex, back_hex, back_alpha = rng.choice(palettes)
            style["primary"] = _to_ass_color(primary_hex, 0)
            style["secondary"] = _to_ass_color("#FFFFFF", 0)
            style["outline"] = _to_ass_color(outline_hex, 0)
            style["back"] = _to_ass_color(back_hex, back_alpha)
        style["font_size"] = max(18, min(160, int(style.get("font_size", base_size)) + rng.choice([-3, -1, 0, 2, 4])))
        style["outline_size"] = max(1, min(8, int(style.get("outline_size", 3)) + rng.choice([-1, 0, 1])))
        style["shadow_size"] = max(0, min(5, int(style.get("shadow_size", 2)) + rng.choice([-1, 0, 1])))
        style["spacing"] = max(-2, min(5, int(style.get("spacing", 0)) + rng.choice([-1, 0, 0, 1, 2])))
        style["scale_x"] = max(88, min(112, int(style.get("scale_x", 100)) + rng.choice([-4, -2, 0, 2, 4])))
        style["italic"] = 1 if rng.random() < 0.12 else int(style.get("italic", 0) or 0)
        style["bold"] = 1

    style["_requested_template_id"] = requested_template_id
    style["_resolved_template_id"] = resolved_template_id
    style["_randomized"] = bool(randomized)
    style["_random_seed"] = random_seed
    return style


def _is_thai_char(ch: str) -> bool:
    """Check if a character is in the Thai Unicode range (Ko Kai to Khomu)."""
    return "\u0e01" <= ch <= "\u0e5b"


def _merge_thai_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge Whisper's character-level Thai token timestamps into word-level timing.
    
    Whisper's Thai tokenizer breaks words into 1-2 char fragments
    (["ใ", "คร", "ร", "อ"] instead of ["ใคร", "รอ"]). This is unusable for
    karaoke-style word-by-word highlighting. PyThaiNLP's `word_tokenize` (newmm
    engine) splits the concatenated text into real Thai words, then maps each
    char fragment's start/end into the parent word's time range.
    
    Returns the input list unchanged if pythainlp is unavailable or no
    valid Thai words are found.
    """
    if not words:
        return []
    try:
        from pythainlp.tokenize import word_tokenize
    except ImportError:
        return words
    # Build char position -> word index map (whitespace-stripped)
    clean_chars: list[int] = []
    clean_text = ""
    for w_idx, w in enumerate(words):
        wtext = str(w.get("word", "")).strip()
        if not wtext:
            continue
        for ch in wtext:
            clean_chars.append(w_idx)
            clean_text += ch
    if not clean_text:
        return words
    # Skip if no Thai characters (English/numbers don't need merge)
    if not any(_is_thai_char(ch) for ch in clean_text):
        return words
    # Use PyThaiNLP to split into real Thai words
    try:
        thai_words = word_tokenize(clean_text, engine="newmm", keep_whitespace=False)
    except Exception:
        return words
    if not thai_words:
        return words
    # Map each char back to its source fragment index
    # Build cumulative char position -> source word index
    word_indices: list[int] = []
    for w_idx, w in enumerate(words):
        wtext = str(w.get("word", "")).strip()
        for _ in wtext:
            word_indices.append(w_idx)
    # For each PyThaiNLP word, find the char range in clean_text
    merged: list[dict[str, Any]] = []
    pos = 0
    for tw in thai_words:
        if not tw or not tw.strip():
            continue
        # Find this word in clean_text starting from pos
        idx = clean_text.find(tw, pos)
        if idx < 0:
            # Skip if not found (shouldn't happen with proper tokenize)
            continue
        end_idx = idx + len(tw)
        if end_idx > len(word_indices):
            end_idx = len(word_indices)
        if idx >= len(word_indices):
            continue
        # Sum the time range of all source fragments in this range
        start = float(words[word_indices[idx]].get("start") or 0.0)
        end = float(words[word_indices[end_idx - 1]].get("end") or start + 0.2)
        merged.append({
            "word": tw,
            "start": start,
            "end": end,
            "probability": float(words[word_indices[idx]].get("probability") or 0.0),
        })
        pos = end_idx
    return merged if merged else words


def _wrap_words(words: list[dict[str, Any]], max_words_per_line: int) -> list[tuple[float, float, str]]:
    lines: list[tuple[float, float, str]] = []
    clean_words = [w for w in words if _normalize_text(w.get("word"))]
    if not clean_words:
        return lines
    idx = 0
    block = max(1, int(max_words_per_line or 4))
    while idx < len(clean_words):
        chunk = clean_words[idx : idx + block]
        start = float(chunk[0].get("start") or 0.0)
        end = float(chunk[-1].get("end") or start + 0.4)
        text = " ".join(_normalize_text(item.get("word")) for item in chunk).strip()
        if text:
            lines.append((start, max(end, start + 0.2), text))
        idx += block
    return lines


def _wrap_words_karaoke(words: list[dict[str, Any]], max_words_per_line: int) -> list[tuple[float, float, str]]:
    lines: list[tuple[float, float, str]] = []
    clean_words = [w for w in words if _normalize_text(w.get("word"))]
    if not clean_words:
        return lines
    idx = 0
    block = max(1, min(3, int(max_words_per_line or 3)))
    while idx < len(clean_words):
        chunk = clean_words[idx : idx + block]
        timed_parts: list[tuple[float, float, str]] = []
        for item in chunk:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start + 0.2)
            text = _normalize_text(item.get("word"))
            if text:
                timed_parts.append((start, max(end, start + 0.05), text))
        if timed_parts:
            line_start = timed_parts[0][0]
            line_end = max(timed_parts[-1][1], line_start + 0.2)
            lines.append((line_start, line_end, _ass_raw_text(_karaoke_ass_text(timed_parts, separator=" "))))
        idx += block
    return lines


def _group_tokens(tokens: list[str], group_size: int, joiner: str) -> list[str]:
    clean = [t for t in tokens if _normalize_text(t)]
    if not clean:
        return []
    size = max(1, int(group_size or 1))
    if size <= 1:
        return clean
    grouped: list[str] = []
    for idx in range(0, len(clean), size):
        chunk = joiner.join(clean[idx : idx + size]).strip()
        if chunk:
            grouped.append(chunk)
    return grouped


def _approx_syllable_chunks_from_words(words: list[str], max_syllables: int) -> list[str]:
    # Fallback when true syllable tokenizer is unavailable: split long words
    # into short Thai chunks, then regroup by requested syllable target.
    clean_words = [_normalize_text(w) for w in words if _normalize_text(w)]
    if not clean_words:
        return []
    approx_chars_per_syllable = 3
    chunk_chars = max(2, min(12, int(round(max_syllables * approx_chars_per_syllable))))
    units: list[str] = []
    for word in clean_words:
        if len(word) <= chunk_chars:
            units.append(word)
            continue
        units.extend(_split_thai_segment_text(word, max_chars=chunk_chars))
    return _group_tokens(units, max_syllables, joiner="")


def _thai_segment_chunks(text: str, settings: AutoSuSettings, log: LogCallback) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    max_syllables = max(1, int(getattr(settings, "max_syllables_per_line", 1) or 1))
    max_words = max(1, int(getattr(settings, "max_words_per_line", 4) or 4))

    try:
        from pythainlp.tokenize import syllable_tokenize, word_tokenize  # type: ignore
    except Exception as exc:
        syllable_tokenize = None  # type: ignore[assignment]
        word_tokenize = None  # type: ignore[assignment]
        log(f"[AutoSu][WARN] pythainlp unavailable ({exc}); fallback tokenization enabled")

    # Primary path: Thai syllable tokenize, grouped by max_syllables_per_line.
    if callable(syllable_tokenize):
        try:
            try:
                raw_syllables = syllable_tokenize(normalized, engine="dict")
            except TypeError:
                raw_syllables = syllable_tokenize(normalized)
            syllables = [_normalize_text(tok) for tok in raw_syllables]
            syllables = [tok for tok in syllables if tok]
            grouped = _group_tokens(syllables, max_syllables, joiner="")
            if grouped:
                return grouped
        except Exception as exc:
            log(f"[AutoSu][WARN] syllable tokenizer failed ({exc}); falling back to word tokenizer")

    # Fallback path with Thai word tokenize.
    if callable(word_tokenize):
        try:
            words = [_normalize_text(tok) for tok in word_tokenize(normalized, engine="newmm")]
            words = [tok for tok in words if tok]
            # If syllable tokenizer is unavailable, approximate syllable-sized chunks
            # from Thai words so UI line settings still affect output granularity.
            grouped = _approx_syllable_chunks_from_words(words, max_syllables=max_syllables)
            if grouped:
                return grouped
        except Exception as exc:
            log(f"[AutoSu][WARN] word tokenizer failed ({exc}); falling back to char tokenizer")

    # Last fallback: punctuation + fixed char slicing.
    max_chars = max(2, min(24, int(round(max_syllables * 3))))
    log(f"[AutoSu][WARN] using last-resort char fallback max_chars={max_chars}")
    return _split_thai_segment_text(normalized, max_chars=max_chars)

def _thai_syllables_individual(text: str, log: LogCallback) -> list[str]:
    """Return individual Thai syllables (no grouping) for per-word karaoke timing.

    Unlike _thai_segment_chunks (which groups by max_syllables_per_line for display),
    this returns 1-element-per-syllable so each word can have its own \kf duration.
    """
    normalized = _normalize_text(text)
    if not normalized:
        return []
    try:
        from pythainlp.tokenize import syllable_tokenize, word_tokenize  # type: ignore
    except Exception as exc:
        log(f"[AutoSu][WARN] pythainlp unavailable ({exc}); cannot split Thai syllables")
        return [normalized]
    if callable(syllable_tokenize):
        try:
            try:
                raw = syllable_tokenize(normalized, engine="dict")
            except TypeError:
                raw = syllable_tokenize(normalized)
            syllables = [_normalize_text(tok) for tok in raw if _normalize_text(tok)]
            if syllables:
                return syllables
        except Exception as exc:
            log(f"[AutoSu][WARN] syllable_tokenize failed ({exc}); falling back to word_tokenize")
    if callable(word_tokenize):
        try:
            words = [_normalize_text(tok) for tok in word_tokenize(normalized, engine="newmm") if _normalize_text(tok)]
            if words:
                return words
        except Exception as exc:
            log(f"[AutoSu][WARN] word_tokenize failed ({exc})")
    return [normalized]




def _split_thai_segment_text(text: str, max_chars: int) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    # Split by common pause punctuation first to keep readable phrases.
    parts = [p.strip() for p in re.split(r"[,.!?;:|/()\[\]{}]+", normalized) if p.strip()]
    if not parts:
        parts = [normalized]

    chunks: list[str] = []
    limit = max(2, min(48, int(max_chars or 20)))
    for part in parts:
        if len(part) <= limit:
            chunks.append(part)
            continue
        idx = 0
        while idx < len(part):
            segment = part[idx : idx + limit].strip()
            if segment:
                chunks.append(segment)
            idx += limit
    return chunks or [normalized]


def _timed_split_segment(
    start: float,
    end: float,
    chunks: list[str],
    *,
    min_each_sec: float = 0.25,
) -> list[tuple[float, float, str]]:
    if not chunks:
        return []
    duration = max(0.2, float(end) - float(start))
    count = max(1, len(chunks))
    slot = max(min_each_sec, duration / count)

    cues: list[tuple[float, float, str]] = []
    cur = float(start)
    for idx, chunk in enumerate(chunks):
        next_t = cur + slot
        # Force last chunk to end exactly at segment end.
        if idx == count - 1:
            next_t = float(end)
        next_t = max(next_t, cur + 0.2)
        cues.append((cur, next_t, chunk))
        cur = next_t
    return cues


def _timed_split_segment_karaoke(
    start: float,
    end: float,
    chunks: list[str],
    *,
    max_units_per_line: int,
    min_each_sec: float = 0.25,
) -> list[tuple[float, float, str]]:
    timed = _timed_split_segment(start, end, chunks, min_each_sec=min_each_sec)
    if not timed:
        return []
    block = max(1, min(3, int(max_units_per_line or 3)))
    cues: list[tuple[float, float, str]] = []
    for idx in range(0, len(timed), block):
        parts = timed[idx : idx + block]
        line_start = parts[0][0]
        line_end = max(parts[-1][1], line_start + 0.2)
        cues.append((line_start, line_end, _ass_raw_text(_karaoke_ass_text(parts, separator=""))))
    return cues


def _strip_think_and_code_fence(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = cleaned.strip()
    if cleaned.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        if match:
            cleaned = match.group(1).strip()
    return cleaned


def _extract_json_object_text(text: str) -> str:
    cleaned = _strip_think_and_code_fence(text)
    if not cleaned:
        return ""
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _parse_minimax_spellfix_content(raw_content: str, expected_count: int) -> list[str] | None:
    json_text = _extract_json_object_text(raw_content)
    if not json_text:
        return None
    try:
        payload = json.loads(json_text)
    except Exception:
        return None
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(lines, list) or len(lines) != expected_count:
        return None
    by_id: dict[int, str] = {}
    for item in lines:
        if not isinstance(item, dict):
            return None
        try:
            idx = int(item.get("id"))
        except Exception:
            return None
        if idx < 1 or idx > expected_count:
            return None
        corrected = _normalize_text(item.get("text"))
        if not corrected:
            return None
        by_id[idx] = corrected
    if len(by_id) != expected_count:
        return None
    return [by_id[i] for i in range(1, expected_count + 1)]


def _minimax_spellfix_batch(
    texts: list[str],
    *,
    language_hint: str,
    log: LogCallback,
) -> list[str] | None:
    if not texts:
        return []

    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        return None

    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io").strip().rstrip("/")
    model = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7").strip() or "MiniMax-M2.7"
    timeout_sec = max(8.0, min(180.0, _env_float("AUTOSU_MINIMAX_TIMEOUT_SEC", 60.0)))
    temperature = max(0.0, min(1.0, _env_float("AUTOSU_MINIMAX_TEMPERATURE", 0.15)))
    top_p = max(0.0, min(1.0, _env_float("AUTOSU_MINIMAX_TOP_P", 0.95)))
    max_tokens = max(64, min(2048, _env_int("AUTOSU_MINIMAX_MAX_COMPLETION_TOKENS", 1024)))

    lines_payload = [{"id": idx + 1, "text": text} for idx, text in enumerate(texts)]
    system_prompt = (
        "You are a subtitle spell-correction engine. "
        "Correct typos and obvious ASR spelling errors only. "
        "Do not translate and do not rewrite style. "
        "Return strict JSON only: "
        "{\"lines\":[{\"id\":1,\"text\":\"...\"}]}. "
        "Keep exactly the same number of lines and ids."
    )
    user_prompt = {
        "language_hint": language_hint or "auto",
        "rules": [
            "preserve line count exactly",
            "preserve semantic meaning",
            "do not include timestamps",
            "return JSON only",
        ],
        "lines": lines_payload,
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "stream": False,
        "temperature": temperature,
        "top_p": top_p,
        "max_completion_tokens": max_tokens,
    }
    endpoint = f"{base_url}/v1/chat/completions"
    request_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        endpoint,
        data=request_data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlrequest.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            if int(getattr(resp, "status", 200)) != 200:
                log(f"[AutoSu] minimax spell-fix http status={getattr(resp, 'status', 'n/a')}")
                return None
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = str(exc)
        log(f"[AutoSu] minimax spell-fix HTTPError: {detail[-240:]}")
        return None
    except Exception as exc:
        log(f"[AutoSu] minimax spell-fix request failed: {exc}")
        return None

    try:
        payload = json.loads(raw)
    except Exception:
        log("[AutoSu] minimax spell-fix response is not JSON")
        return None

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        log("[AutoSu] minimax spell-fix response missing choices")
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "")

    corrected = _parse_minimax_spellfix_content(content, expected_count=len(texts))
    if corrected is None:
        log("[AutoSu] minimax spell-fix parse failed, keep original text")
        return None
    return corrected


def _apply_minimax_spellfix(cues: list[tuple[float, float, str]], settings: AutoSuSettings, log: LogCallback) -> list[tuple[float, float, str]]:
    if not cues:
        return cues
    if not bool(getattr(settings, "ai_spellfix_before_burn", False)):
        return cues
    if not _env_flag("AUTOSU_MINIMAX_SPELLFIX_ENABLED", "1"):
        return cues
    if not os.getenv("MINIMAX_API_KEY", "").strip():
        log("[AutoSu] minimax spell-fix skipped: MINIMAX_API_KEY not set")
        return cues

    batch_size = max(8, min(80, _env_int("AUTOSU_MINIMAX_SPELLFIX_BATCH_SIZE", 40)))
    fixed: list[tuple[float, float, str]] = []
    changed = 0
    attempted_lines = 0

    for start_idx in range(0, len(cues), batch_size):
        batch = cues[start_idx : start_idx + batch_size]
        texts = [_spellfix_visible_text(str(item[2])) for item in batch]
        attempted_lines += len(texts)
        corrected = _minimax_spellfix_batch(texts, language_hint=settings.language, log=log)
        if not corrected or len(corrected) != len(texts):
            fixed.extend(batch)
            continue
        for (start, end, original), new_text in zip(batch, corrected):
            new_text_norm = _normalize_text(new_text)
            original_visible = _spellfix_visible_text(original)
            if new_text_norm and new_text_norm != original_visible:
                changed += 1
            if not new_text_norm or new_text_norm == original_visible:
                fixed_text = original
            else:
                fixed_text = _restore_spellfixed_karaoke_text(original, new_text_norm)
            fixed.append((start, end, fixed_text))

    log(f"[AutoSu] minimax spell-fix done: changed={changed}/{attempted_lines}")
    return fixed


def _is_whisper_alignment_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    needles = (
        "cannot reshape tensor",
        "key and value must have the same sequence length",
        "word timestamp",
        "word_timestamps",
        "alignment",
        "dtw",
    )
    return any(needle in message for needle in needles)


def _classify_autosu_error(message: str) -> str:
    text = str(message or "").lower()
    if text.startswith("skip_no_speech"):
        return "skip_no_speech"
    if text.startswith("skip_no_usable_cues"):
        return "skip_no_usable_cues"
    if "cancel" in text:
        return "cancelled"
    if _is_whisper_alignment_error(Exception(text)):
        return "whisper_alignment"
    if "cuda out of memory" in text or "out of memory" in text:
        return "gpu_oom"
    if "ffmpeg" in text:
        return "ffmpeg_burn"
    if "missing output" in text:
        return "missing_output"
    return "error"


def _extract_cues(
    input_video: Path,
    settings: AutoSuSettings,
    log: LogCallback,
    *,
    model_slot: WhisperModelSlot | None = None,
) -> tuple[list[tuple[float, float, str]], str, dict[str, Any]]:
    # whisper is imported lazily inside _get_whisper_model_cached.
    try:
        import torch
    except Exception as exc:
        return [], f"python-torch not available: {exc}", {}

    (APP_DIR / "models" / "whisper").mkdir(exist_ok=True)
    slot = model_slot or get_active_slot()
    validation = validate_slot(slot.slot_id, verify_hash=False, check_cuda=False)
    if not validation["ok"]:
        raise RuntimeError(f"Whisper slot {slot.slot_id} is invalid: {'; '.join(validation['errors'])}")
    if require_cuda() and (slot.device != "cuda" or not torch.cuda.is_available()):
        raise RuntimeError(
            "❌ Subtitle feature requires GPU. No CUDA detected. "
            "Set APP_SUBTITLE_PIPELINE_ENABLED=false to disable subtitle jobs, "
            "or run on a GPU-enabled server."
        )
    # The lock is released as soon as transcribe/retry/allocator cleanup ends;
    # cue grouping, spell-fix and FFmpeg work remain concurrent.
    model_telemetry = {
        "whisper_model_slot": slot.slot_id,
        "whisper_model_label": slot.label,
        "whisper_backend": slot.backend,
        "whisper_model_fingerprint": slot.fingerprint,
        "whisper_device": slot.device,
        "whisper_gpu_only_required": bool(slot.strict_gpu),
        "whisper_gpu_only_verified": False,
        "whisper_cpu_fallback_used": False,
        "whisper_fallback_policy": "fail_closed" if slot.strict_gpu else "alignment_retry",
    }
    log(
        "[AutoSu] whisper policy "
        f"slot={slot.slot_id} backend={slot.backend} device={slot.device} "
        f"fingerprint={slot.fingerprint[:12]}"
    )
    result = _transcribe_with_cached_whisper(
        input_video,
        slot=slot,
        language=settings.language,
        log=log,
    )
    runtime_telemetry = result.get("_whisper_runtime")
    if isinstance(runtime_telemetry, dict):
        model_telemetry.update(runtime_telemetry)
    segments = list(result.get("segments") or [])
    if not segments:
        return [], "skip_no_speech: whisper produced no segments", model_telemetry

    detected_language = str(result.get("language") or settings.language or "").strip().lower()
    runtime_word_timestamps = model_telemetry.get("whisper_word_timestamps")
    has_segment_pseudo_words = any(
        len(words := list(segment.get("words") or [])) == 1
        and _normalize_text(words[0].get("word")) == _normalize_text(segment.get("text"))
        and (
            len(_normalize_text(segment.get("text")).split()) > 1
            or len(_normalize_text(segment.get("text"))) > 50
        )
        for segment in segments
    )
    segment_fallback = runtime_word_timestamps is False or has_segment_pseudo_words
    # Thai word timestamps are often split into very short token cues that flicker too fast.
    # Segment-timestamp fallback also exposes a whole sentence as one pseudo-word;
    # treating that as granular timing defeats the configured syllable limit.
    prefer_segment_cues = (
        (settings.language == "th")
        or detected_language.startswith("th")
        or segment_fallback
    )
    timing_mode = "segment-fallback" if segment_fallback else ("segment" if prefer_segment_cues else "word")
    log(
        "[AutoSu] subtitle timing mode="
        f"{timing_mode} "
        f"(lang={detected_language or 'unknown'})"
    )

    cues: list[tuple[float, float, str]] = []
    karaoke_enabled = _is_karaoke_template(settings)
    max_words = max(1, int(settings.max_words_per_line or 4))
    if karaoke_enabled:
        max_words = min(3, max_words)
    max_syllables = max(1, int(getattr(settings, "max_syllables_per_line", 1) or 1))
    karaoke_units_per_cue = 1 if prefer_segment_cues else max_words
    if karaoke_enabled:
        log(
            "[AutoSu] karaoke_highlight enabled: ASS \\kf, "
            f"max_units_per_line={karaoke_units_per_cue}"
        )
    if prefer_segment_cues:
        log(
            "[AutoSu] thai grouping config: "
            f"max_syllables_per_line={max_syllables} max_words_per_line={max_words}"
        )
    thai_merge_enabled = detected_language.startswith("th") or settings.language.lower().startswith("th")
    for seg in segments:
        words = list(seg.get("words") or [])
        if thai_merge_enabled and words:
            words = _merge_thai_words(words)
        if words and not prefer_segment_cues:
            if karaoke_enabled:
                cues.extend(_wrap_words_karaoke(words, max_words_per_line=max_words))
            else:
                cues.extend(_wrap_words(words, max_words_per_line=max_words))
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start + 1.0)
        text = _normalize_text(seg.get("text"))
        if text:
            if prefer_segment_cues:
                if karaoke_enabled:
                    # Per-word Thai karaoke: get INDIVIDUAL syllables so each word gets
                    # its own \kf timing (highlight follows each word). Display line
                    # grouping still happens inside _timed_split_segment_karaoke.
                    chunks = _thai_syllables_individual(text, log=log)
                else:
                    # Non-karaoke path keeps max_syllables_per_line grouping
                    chunks = _thai_segment_chunks(text, settings=settings, log=log)
                if karaoke_enabled:
                    cues.extend(
                        _timed_split_segment_karaoke(
                            start,
                            max(end, start + 0.2),
                            chunks,
                            # _thai_syllables_individual returns individual syllables.
                            max_units_per_line=1,
                        )
                    )
                else:
                    cues.extend(_timed_split_segment(start, max(end, start + 0.2), chunks))
            else:
                if karaoke_enabled:
                    tokens = [_normalize_text(token) for token in text.split() if _normalize_text(token)]
                    parts: list[tuple[float, float, str]] = []
                    if tokens:
                        duration = max(0.2, float(end) - float(start))
                        slot = duration / max(1, len(tokens))
                        cur = float(start)
                        for idx, token in enumerate(tokens):
                            next_t = float(end) if idx == len(tokens) - 1 else cur + slot
                            next_t = max(next_t, cur + 0.05)
                            parts.append((cur, next_t, token))
                            cur = next_t
                    if parts:
                        cues.extend(
                            (
                                parts[idx][0],
                                max(parts[min(idx + max_words - 1, len(parts) - 1)][1], parts[idx][0] + 0.2),
                                _ass_raw_text(_karaoke_ass_text(parts[idx : idx + max_words], separator=" ")),
                            )
                            for idx in range(0, len(parts), max_words)
                        )
                    else:
                        cues.append((start, max(end, start + 0.2), text))
                else:
                    cues.append((start, max(end, start + 0.2), text))

    media_duration = _ffprobe_media_duration(input_video)
    if media_duration > 0:
        valid_cues: list[tuple[float, float, str]] = []
        dropped_out_of_range = 0
        clipped_to_duration = 0
        for start, end, text in cues:
            if end <= 0 or start >= media_duration:
                dropped_out_of_range += 1
                continue
            clipped_start = max(0.0, float(start))
            clipped_end = min(media_duration, float(end))
            if clipped_end <= clipped_start:
                dropped_out_of_range += 1
                continue
            if clipped_start != float(start) or clipped_end != float(end):
                clipped_to_duration += 1
            valid_cues.append((clipped_start, clipped_end, text))
        cues = valid_cues
        model_telemetry.update(
            {
                "subtitle_source_duration_sec": round(media_duration, 6),
                "subtitle_cues_dropped_out_of_range": dropped_out_of_range,
                "subtitle_cues_clipped_to_duration": clipped_to_duration,
            }
        )
        if dropped_out_of_range or clipped_to_duration:
            log(
                "[AutoSu] subtitle duration guard "
                f"duration={media_duration:.3f}s dropped={dropped_out_of_range} "
                f"clipped={clipped_to_duration} remaining={len(cues)}"
            )

    if not cues:
        if settings.speech_check_before_burn:
            return [], "skip_no_usable_cues: speech check failed: no usable cues", model_telemetry
        return [], "skip_no_usable_cues: no usable cues", model_telemetry
    return cues, "", model_telemetry


def _write_srt(cues: list[tuple[float, float, str]], srt_path: Path) -> None:
    rows: list[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        rows.append(str(idx))
        rows.append(f"{_sec_to_srt(start)} --> {_sec_to_srt(end)}")
        rows.append(text)
        rows.append("")
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text("\n".join(rows), encoding="utf-8")


def _ffprobe_media_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    ok, out = _run_ffmpeg_cmd(cmd, log_func=None)
    if not ok:
        return 0.0
    try:
        text = str(out or "{}")
        json_start = text.find("{")
        if json_start > 0:
            text = text[json_start:]
        data = json.loads(text or "{}")
        duration = float((data.get("format") or {}).get("duration") or 0.0)
        return duration if duration > 0 else 0.0
    except Exception as exc:
        print(f"[AutoSu][WARN] ffprobe duration parse failed path={path}: {exc}")
        return 0.0


def _ffprobe_video_size(path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    ok, out = _run_ffmpeg_cmd(cmd, log_func=None)
    if not ok:
        return 1080, 1920
    try:
        text = str(out or "{}")
        json_start = text.find("{")
        if json_start > 0:
            text = text[json_start:]
        data = json.loads(text or "{}")
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width > 0 and height > 0:
            return width, height
    except Exception as exc:
        print(f"[AutoSu][WARN] ffprobe size parse failed path={path}: {exc}")
    return 1080, 1920


def _write_ass(
    cues: list[tuple[float, float, str]],
    ass_path: Path,
    input_video: Path,
    settings: AutoSuSettings,
) -> tuple[Path, int, int, dict[str, Any]]:
    width, height = _ffprobe_video_size(input_video)
    width = max(64, int(width))
    height = max(64, int(height))
    font_name = "Arial"
    if settings.font_path:
        font_name = Path(settings.font_path).stem or "Arial"

    box_opacity = max(0, min(100, int(settings.background_opacity or 25)))
    use_box_style = box_opacity >= 70
    outline_size = 2 if use_box_style else max(1, min(3, int(round(1 + (box_opacity / 60.0)))))
    shadow_size = 0 if use_box_style else 1

    position_percent = max(0, min(100, int(settings.position_percent or 100)))
    # Match UI preview semantics: percent represents the visual center point from top.
    y = int(round((position_percent / 100.0) * height))
    min_y = max(26, int(round(height * 0.05)))
    max_y = min(height - 26, int(round(height * 0.95)))
    y = max(min_y, min(max_y, y))
    x = int(round(width / 2))

    primary = _to_ass_color(settings.font_color, 0)
    secondary = primary
    outline = _to_ass_color(settings.border_color, 0)
    back = _to_ass_color(settings.background_color, box_opacity)
    font_size = max(18, min(160, int(settings.font_size or 44)))
    bold = 0
    italic = 0
    spacing = 0
    scale_x = 100
    border_style = 3 if use_box_style else 1
    template_style = _subtitle_template_style(settings, input_video=input_video)
    primary = template_style.get("primary", primary)
    secondary = template_style.get("secondary", secondary)
    outline = template_style.get("outline", outline)
    back = template_style.get("back", back)
    font_size = max(18, min(160, int(template_style.get("font_size", font_size))))
    outline_size = max(0, min(8, int(template_style.get("outline_size", outline_size))))
    shadow_size = max(0, min(5, int(template_style.get("shadow_size", shadow_size))))
    border_style = 3 if int(template_style.get("border_style", border_style)) == 3 else 1
    bold = 1 if int(template_style.get("bold", bold)) else 0
    italic = 1 if int(template_style.get("italic", italic)) else 0
    spacing = max(-5, min(8, int(template_style.get("spacing", spacing))))
    scale_x = max(80, min(125, int(template_style.get("scale_x", scale_x))))
    template_info = {
        "requested_template_id": str(template_style.get("_requested_template_id", "default") or "default"),
        "resolved_template_id": str(template_style.get("_resolved_template_id", "default") or "default"),
        "randomized": bool(template_style.get("_randomized", False)),
        "random_seed": str(template_style.get("_random_seed", "") or ""),
    }

    rows: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"; AutoSuTemplateRequested: {template_info['requested_template_id']}",
        f"; AutoSuTemplateResolved: {template_info['resolved_template_id']}",
        f"; AutoSuTemplateRandomized: {str(template_info['randomized']).lower()}",
        f"; AutoSuTemplateRandomSeed: {template_info['random_seed']}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Default,"
        f"{font_name},{font_size},{primary},{secondary},{outline},{back},"
        f"{bold},{italic},0,0,{scale_x},100,{spacing},0,{border_style},{outline_size},{shadow_size},5,20,20,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for start, end, text in cues:
        safe_text = _strip_ass_raw_prefix(text) if _is_ass_raw_text(text) else _ass_escape_text(text)
        if not safe_text:
            continue
        s = _sec_to_ass(start)
        e = _sec_to_ass(max(end, start + 0.2))
        rows.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{{\\an5\\pos({x},{y})}}{safe_text}")

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text("\n".join(rows), encoding="utf-8")
    return ass_path, width, height, template_info


def _subtitle_alignment(settings: AutoSuSettings) -> tuple[int, int]:
    position_percent = max(0, min(100, int(settings.position_percent or 100)))
    # Use finer vertical placement from position_percent.
    # top    (0..45): Alignment=8 with increasing top margin
    # center (46..54): Alignment=5
    # bottom (55..100): Alignment=2 with increasing bottom margin
    if position_percent <= 45:
        margin_v = int(round(18 + (position_percent / 45.0) * 240))
        return 8, margin_v
    if position_percent >= 55:
        margin_v = int(round(18 + ((100 - position_percent) / 45.0) * 240))
        return 2, margin_v
    return 5, 20


def _build_subtitles_filter(srt_path: Path, settings: AutoSuSettings) -> str:
    align, margin_v = _subtitle_alignment(settings)
    font_name = "Arial"
    if settings.font_path:
        font_name = Path(settings.font_path).stem or "Arial"
    box_opacity = max(0, min(100, int(settings.background_opacity or 45)))
    # BorderStyle=3 draws an opaque full-line box and can look like giant black blocks.
    # Keep readability by default with outline text, and only enable boxed style on very high opacity.
    use_box_style = box_opacity >= 70
    outline_size = 2 if use_box_style else max(1, min(4, int(round(1 + (box_opacity / 45.0)))))
    shadow_size = 0 if use_box_style else max(0, min(2, int(round(box_opacity / 50.0))))
    style = ",".join(
        [
            f"FontName={_ffmpeg_escape_filter_value(font_name)}",
            f"FontSize={max(18, min(160, int(settings.font_size or 56)))}",
            f"PrimaryColour={_to_ass_color(settings.font_color, 0)}",
            f"OutlineColour={_to_ass_color(settings.border_color, 0)}",
            f"BackColour={_to_ass_color(settings.background_color, box_opacity)}",
            f"BorderStyle={3 if use_box_style else 1}",
            f"Outline={outline_size}",
            f"Shadow={shadow_size}",
            f"Alignment={align}",
            f"MarginV={margin_v}",
        ]
    )
    # Use the named filename option. FFmpeg 6 rejects the positional filename
    # when another subtitles option (for example fontsdir) follows it.
    filter_value = f"subtitles=filename='{_ffmpeg_filter_path(srt_path)}':force_style='{style}'"
    font_dir = str(Path(settings.font_path).expanduser().resolve().parent) if settings.font_path else ""
    if font_dir:
        escaped_dir = _ffmpeg_filter_path(Path(font_dir))
        filter_value = f"{filter_value}:fontsdir='{escaped_dir}'"
    return filter_value


def _run_ffmpeg_burn(
    input_video: Path,
    srt_path: Path,
    cues: list[tuple[float, float, str]],
    output_video: Path,
    settings: AutoSuSettings,
    *,
    initial_encoder: str,
    encoder_chain: list[str],
    log: LogCallback,
) -> tuple[bool, str, str, bool, dict[str, Any]]:
    ffmpeg_exe = get_ffmpeg_path()
    ass_path: Path | None = None
    template_info: dict[str, Any] = {}
    try:
        ass_path, play_w, play_h, template_info = _write_ass(cues, srt_path.with_suffix(".ass"), input_video, settings)
        # Keep the ASS path explicit; positional subtitles paths break when
        # fontsdir is appended on the live Ubuntu FFmpeg build.
        filter_expr = f"subtitles=filename='{_ffmpeg_filter_path(ass_path)}'"
        font_dir = str(Path(settings.font_path).expanduser().resolve().parent) if settings.font_path else ""
        if font_dir:
            filter_expr = f"{filter_expr}:fontsdir='{_ffmpeg_filter_path(Path(font_dir))}'"
        log(
            "[AutoSu] burn positioning via ASS "
            f"position_percent={settings.position_percent} play_res={play_w}x{play_h}"
            f" template={template_info.get('requested_template_id', 'default')}"
            f"->{template_info.get('resolved_template_id', 'default')}"
            f" randomized={template_info.get('randomized', False)}"
        )
    except Exception as exc:
        log(f"[AutoSu] ASS prepare failed -> fallback legacy force_style: {exc}")
        filter_expr = _build_subtitles_filter(srt_path, settings)
    attempted: list[str] = []
    chain = [enc for enc in encoder_chain if _normalize_encoder_name(enc)]
    if not chain:
        chain = ["libx264"]
    for idx, candidate in enumerate(chain):
        enc = _normalize_encoder_name(candidate) or "libx264"
        attempted.append(enc)
        cmd = [
            str(ffmpeg_exe),
            "-y",
            "-i",
            str(input_video),
            "-vf",
            filter_expr,
            *_build_video_encode_args(enc),
            "-c:a",
            "copy",
            str(output_video),
        ]
        log(f"[AutoSu] ffmpeg burn attempt encoder={enc}")
        try:
            ok, output = _run_ffmpeg_cmd(cmd, log_func=log)
        except Exception as exc:
            if idx >= len(chain) - 1:
                return False, str(exc), enc, (enc != _normalize_encoder_name(initial_encoder)), template_info
            continue
        output = str(output or "").strip()
        if ok:
            if output:
                log("[AutoSu] ffmpeg burn finished")
            return True, "", enc, (enc != _normalize_encoder_name(initial_encoder)), template_info
        if idx < len(chain) - 1:
            log(f"[AutoSu][WARN] encoder failed={enc}, fallback next")
            continue
        return False, output[-1200:] if output else "ffmpeg failed", enc, (enc != _normalize_encoder_name(initial_encoder)), template_info
    return False, "ffmpeg failed", chain[-1], (chain[-1] != _normalize_encoder_name(initial_encoder)), template_info


def run_autosu_on_outputs(
    output_paths: list[str],
    settings: AutoSuSettings,
    *,
    output_root: str | Path | None = None,
    log_cb: LogCallback | None = None,
    cancel_check: CancelCheck | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    status_callback: Callable[[str], None] | None = None,
    trim_settings: Any | None = None,
) -> dict[str, Any]:
    log = log_cb or _noop
    cancelled = cancel_check or _cancel_false
    progress = progress_callback or (lambda idx, total, src: None)
    status = status_callback or _noop
    ffmpeg_exe = get_ffmpeg_path()
    initial_encoder, encoder_chain = _resolve_autosu_encoder_chain(ffmpeg_exe)
    status("เตรียมระบบเข้ารหัสวิดีโอ (" + initial_encoder + ")")
    log(
        "[AutoSu] encoder chain resolved "
        f"initial={initial_encoder} chain={encoder_chain}"
    )
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    generated_outputs: list[str] = []
    used_encoders: list[str] = []
    fallback_used = False
    resolved_output_root = Path(output_root).resolve() if output_root else None
    name_hits: dict[str, int] = {}
    run_model_slot = get_active_slot()
    log(
        "[AutoSu] run model snapshot "
        f"slot={run_model_slot.slot_id} backend={run_model_slot.backend} "
        f"fingerprint={run_model_slot.fingerprint[:12]}"
    )

    total = len(output_paths)
    for idx, raw_path in enumerate(output_paths, start=1):
        if cancelled():
            errors.append("cancelled before subtitle burn")
            break
        progress(idx, total, str(raw_path))  # signal: starting this video
        src = Path(str(raw_path or "")).resolve()
        if not src.exists() or not src.is_file():
            msg = f"skip missing output: {src}"
            log(f"[AutoSu] {msg}")
            errors.append(msg)
            progress(idx, len(output_paths), str(src))
            continue
        base_stem = src.stem or "video"
        stem_key = base_stem.lower()
        stem_hit = int(name_hits.get(stem_key, 0))
        name_hits[stem_key] = stem_hit + 1
        stem_suffix = f"_{stem_hit + 1}" if stem_hit > 0 else ""
        output_stem = f"{base_stem}{stem_suffix}"
        withsub_dir = (resolved_output_root / "withsub") if resolved_output_root else (src.parent / "withsub")
        srt_path = withsub_dir / f"{output_stem}.autosu.srt"
        out_path = withsub_dir / f"{output_stem}.autosu.mp4"
        item = {
            "input": str(src),
            "srt_path": str(srt_path),
            "output": str(out_path),
            "ok": False,
            "error": "",
            "initial_encoder": initial_encoder,
            "used_encoder": "",
            "encoder_fallback_used": False,
            "template_requested": _normalize_runner_template_id(getattr(settings, "subtitle_template_id", "default")),
            "template_resolved": "",
            "template_randomized": False,
            "template_random_seed": "",
            "skip_reason": "",
            "error_class": "",
            "whisper_model_slot": "",
            "whisper_model_label": "",
            "whisper_backend": "",
            "whisper_model_fingerprint": "",
            "whisper_device": "",
        }
        try:
            withsub_dir.mkdir(parents=True, exist_ok=True)
            log(f"[AutoSu] transcribing: {src.name}")
            status("กำลังถอดเสียง: " + src.name + " (Whisper Large-v3)")
            cues, err, model_telemetry = _extract_cues(
                src,
                settings,
                log,
                model_slot=run_model_slot,
            )
            item.update(model_telemetry)
            if err:
                item["error"] = err
                item["error_class"] = _classify_autosu_error(err)
                if str(err).startswith("skip_"):
                    item["skip_reason"] = err
                    log(f"[AutoSu][FILE_SKIP] {src.name}: {err}")
                else:
                    log(f"[AutoSu][FILE_FAIL] {src.name}: {err}")
                errors.append(f"{src.name}: {err}")
                items.append(item)
                continue
            if cancelled():
                cancel_msg = "cancelled before spellfix"
                item["error"] = cancel_msg
                item["error_class"] = "cancelled"
                log(f"[AutoSu][FILE_CANCEL] {src.name}: {cancel_msg}")
                errors.append(f"{src.name}: {cancel_msg}")
                items.append(item)
                break
            cues = _apply_minimax_spellfix(cues, settings, log)
            if cancelled():
                cancel_msg = "cancelled before subtitle burn"
                item["error"] = cancel_msg
                item["error_class"] = "cancelled"
                log(f"[AutoSu][FILE_CANCEL] {src.name}: {cancel_msg}")
                errors.append(f"{src.name}: {cancel_msg}")
                items.append(item)
                break
            # ====== Subtitle Trim Silence hook ======
            trim_manifest: dict[str, Any] = {"skipped": True, "reason": "not_requested"}
            burn_input: Path = src
            if trim_settings is not None and bool(getattr(trim_settings, "trim_silence", False)):
                trim_work_dir = (withsub_dir / "trim_work") if resolved_output_root else (src.parent / "withsub" / "trim_work")
                try:
                    burned_input_candidate, trim_manifest = trim_silence_proxy(
                        src,
                        trim_work_dir,
                        trim_settings,
                        log,
                        ffmpeg_exe=ffmpeg_exe,
                    )
                except Exception as exc:
                    log(f"[AutoSu][SUBTITLE_TRIM] exception {src.name}: {exc}")
                    trim_manifest = {"skipped": True, "reason": f"exception:{exc}"}
                    burned_input_candidate = src
                if not bool(trim_manifest.get("skipped", True)) and burned_input_candidate != src:
                    # Re-align cues by subtracting the removed silence duration
                    silences = list(trim_manifest.get("silence_intervals") or [])
                    margin_sec = float(getattr(trim_settings, "trim_silence_margin_sec", 0.0) or 0.0)
                    cues = re_align_cues(cues, silences, margin_sec=margin_sec)
                    burn_input = burned_input_candidate
                    log(
                        f"[AutoSu] using trimmed source for burn: "
                        f"{burn_input.name} after={trim_manifest.get('after_duration', 0):.3f}s"
                    )
                else:
                    burn_input = src
            item["subtitle_trim"] = trim_manifest
            # ====== end trim hook ======
            _write_srt(cues, srt_path)
            log(f"[AutoSu] burning subtitle: {burn_input.name}")
            status("เตรียม ASS พร้อม karaoke effect: " + burn_input.name)
            ok, burn_err, used_encoder, encoder_fallback, template_info = _run_ffmpeg_burn(
                burn_input,
                srt_path,
                cues,
                out_path,
                settings,
                initial_encoder=initial_encoder,
                encoder_chain=encoder_chain,
                log=log,
            )
            item["used_encoder"] = used_encoder
            item["encoder_fallback_used"] = bool(encoder_fallback)
            item["template_requested"] = str(template_info.get("requested_template_id") or item["template_requested"])
            item["template_resolved"] = str(template_info.get("resolved_template_id") or "")
            item["template_randomized"] = bool(template_info.get("randomized", False))
            item["template_random_seed"] = str(template_info.get("random_seed") or "")
            if not ok:
                item["error"] = burn_err
                item["error_class"] = _classify_autosu_error(burn_err)
                log(f"[AutoSu][FILE_FAIL] {src.name}: {burn_err}")
                errors.append(f"{src.name}: {burn_err}")
                items.append(item)
                continue
            item["ok"] = True
            used_encoders.append(_normalize_encoder_name(used_encoder) or "libx264")
            fallback_used = fallback_used or bool(encoder_fallback)
            generated_outputs.append(str(out_path))
        except Exception as exc:
            item["error"] = str(exc)
            item["error_class"] = _classify_autosu_error(str(exc))
            log(f"[AutoSu][FILE_FAIL] {src.name}: {exc}")
            errors.append(f"{src.name}: {exc}")
        items.append(item)

    succeeded = sum(1 for item in items if item.get("ok"))
    failed = sum(1 for item in items if not item.get("ok"))
    normalized_used = [_normalize_encoder_name(enc) for enc in used_encoders if _normalize_encoder_name(enc)]
    if not normalized_used:
        final_used_encoder = _normalize_encoder_name(initial_encoder) or "libx264"
    elif len(set(normalized_used)) == 1:
        final_used_encoder = normalized_used[0]
    else:
        final_used_encoder = "mixed(" + ",".join(sorted(set(normalized_used))) + ")"
    if _whisper_unload_after_run_enabled():
        cache_keys = list(_whisper_cache_snapshot()["keys"])
        _whisper_release_cached_models(
            cache_keys,
            log=log,
            reason="run-complete",
        )
    return {
        "attempted": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "partial": bool(succeeded > 0 and failed > 0),
        "skip_count": sum(1 for item in items if str(item.get("skip_reason") or "").strip()),
        "error_classes": sorted(
            {
                str(item.get("error_class") or "").strip()
                for item in items
                if str(item.get("error_class") or "").strip()
            }
        ),
        "outputs": generated_outputs,
        "items": items,
        "errors": errors,
        "trim_manifests": [item.get("subtitle_trim") for item in items if item.get("subtitle_trim")],
        "trim_applied_count": sum(1 for item in items if item.get("subtitle_trim") and not item["subtitle_trim"].get("skipped", True)),
        "initial_encoder": _normalize_encoder_name(initial_encoder) or "libx264",
        "used_encoder": final_used_encoder,
        "encoder_fallback_used": bool(fallback_used),
        "whisper_cache": _whisper_cache_snapshot(),
    }