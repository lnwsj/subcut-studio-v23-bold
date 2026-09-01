"""Whisper backend adapters selected by the admin model-slot registry."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable

from .whisper_model_registry import WhisperModelSlot, allow_remote_download


LogCallback = Callable[[str], None]


def _timestamp_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        start = float(value[0])
        end = float(value[1])
    except (TypeError, ValueError):
        return None
    return max(0.0, start), max(start + 0.01, end)


def _chunks_to_segments(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Transformers word chunks to the OpenAI Whisper result shape."""
    timed: list[dict[str, Any]] = []
    for chunk in chunks:
        timestamp = _timestamp_pair(chunk.get("timestamp"))
        text = str(chunk.get("text") or "").strip()
        if timestamp is None or not text:
            continue
        timed.append({"start": timestamp[0], "end": timestamp[1], "word": text})

    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for word in timed:
        starts_new = bool(
            current
            and (
                len(current) >= 12
                or word["start"] - current[-1]["end"] > 0.8
                or word["end"] - current[0]["start"] > 6.0
            )
        )
        if starts_new:
            segments.append(
                {
                    "start": current[0]["start"],
                    "end": current[-1]["end"],
                    "text": " ".join(item["word"] for item in current),
                    "words": list(current),
                }
            )
            current = []
        current.append(word)
    if current:
        segments.append(
            {
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": " ".join(item["word"] for item in current),
                "words": list(current),
            }
        )
    return segments


@dataclass(slots=True)
class TransformersWhisperAdapter:
    model: Any
    pipeline: Any
    slot: WhisperModelSlot

    def _assert_gpu_resident(self) -> dict[str, Any]:
        import torch

        if not self.slot.strict_gpu:
            return {
                "whisper_gpu_only_required": False,
                "whisper_gpu_only_verified": False,
                "whisper_cpu_fallback_used": False,
            }
        if self.slot.device.lower() != "cuda" or not torch.cuda.is_available():
            raise RuntimeError("Model_002 strict GPU policy failed: CUDA is unavailable")
        parameter_devices = {str(parameter.device) for parameter in self.model.parameters()}
        buffer_devices = {str(buffer.device) for buffer in self.model.buffers()}
        non_cuda = sorted(
            device for device in parameter_devices | buffer_devices if not device.startswith("cuda")
        )
        if non_cuda:
            raise RuntimeError(
                "Model_002 strict GPU policy failed: model tensors found outside CUDA: "
                + ", ".join(non_cuda)
            )
        pipeline_device = str(getattr(self.pipeline, "device", ""))
        if pipeline_device and not pipeline_device.startswith("cuda"):
            raise RuntimeError(
                f"Model_002 strict GPU policy failed: pipeline device is {pipeline_device}"
            )
        return {
            "whisper_gpu_only_required": True,
            "whisper_gpu_only_verified": True,
            "whisper_cpu_fallback_used": False,
            "whisper_cuda_device": str(torch.cuda.current_device()),
            "whisper_cuda_device_name": str(torch.cuda.get_device_name(torch.cuda.current_device())),
            "whisper_parameter_devices": sorted(parameter_devices),
            "whisper_buffer_devices": sorted(buffer_devices),
        }

    def transcribe(self, input_path: str, **kwargs: Any) -> dict[str, Any]:
        import torch

        word_timestamps = bool(kwargs.get("word_timestamps", True))
        language = str(kwargs.get("language") or "").strip()
        generate_kwargs: dict[str, Any] = {"task": "transcribe"}
        if language and language != "auto":
            generate_kwargs["language"] = language
        gpu_telemetry = self._assert_gpu_resident()
        if self.slot.strict_gpu:
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            payload = self.pipeline(
                input_path,
                return_timestamps="word" if word_timestamps else True,
                generate_kwargs=generate_kwargs,
            )
        if self.slot.strict_gpu:
            torch.cuda.synchronize()
            gpu_telemetry.update(self._assert_gpu_resident())
            gpu_telemetry.update(
                {
                    "whisper_cuda_memory_allocated_mb": round(
                        torch.cuda.memory_allocated() / (1024 * 1024), 2
                    ),
                    "whisper_cuda_peak_memory_mb": round(
                        torch.cuda.max_memory_allocated() / (1024 * 1024), 2
                    ),
                    "whisper_word_timestamps": word_timestamps,
                    "whisper_fallback_policy": "fail_closed",
                }
            )
        chunks = list(payload.get("chunks") or []) if isinstance(payload, dict) else []
        segments = _chunks_to_segments(chunks)
        if not segments and isinstance(payload, dict) and str(payload.get("text") or "").strip():
            segments = [{"start": 0.0, "end": 0.01, "text": str(payload["text"]).strip(), "words": []}]
        return {
            "text": str(payload.get("text") or "") if isinstance(payload, dict) else "",
            "segments": segments,
            "language": language or "auto",
            "_whisper_runtime": gpu_telemetry,
        }

    def cpu(self) -> None:
        if hasattr(self.model, "to"):
            self.model.to("cpu")


def _load_openai_whisper(slot: WhisperModelSlot, download_root: Path) -> Any:
    import whisper

    return whisper.load_model(
        slot.source,
        device=slot.device,
        download_root=str(download_root),
    )


def _load_transformers_whisper(slot: WhisperModelSlot) -> TransformersWhisperAdapter:
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    dtype = getattr(torch, slot.dtype, None)
    if dtype is None:
        raise RuntimeError(f"unsupported torch dtype for {slot.slot_id}: {slot.dtype}")
    if slot.strict_gpu:
        if slot.device.lower() != "cuda" or not torch.cuda.is_available():
            raise RuntimeError("Model_002 strict GPU policy failed: CUDA is unavailable")
        if slot.dtype.lower() == "bfloat16" and not torch.cuda.is_bf16_supported():
            raise RuntimeError("Model_002 strict GPU policy failed: CUDA device does not support bfloat16")
    local_only = not allow_remote_download()
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        slot.source,
        torch_dtype=dtype,
        low_cpu_mem_usage=True,
        use_safetensors=True,
        local_files_only=local_only,
    )
    model.to(slot.device)
    model.eval()
    # Patch generation_config for HF transformers >= 5.x compatibility
    # (Pathumma config doesn't have all attributes needed for return_timestamps)
    if not hasattr(model.generation_config, 'no_timestamps_token_id') or model.generation_config.no_timestamps_token_id is None:
        model.generation_config.no_timestamps_token_id = 50364
    if not hasattr(model.generation_config, 'lang_to_id') or not model.generation_config.lang_to_id:
        WHISPER_LANGS = ['en', 'zh', 'de', 'es', 'ru', 'ko', 'fr', 'ja', 'pt', 'tr', 'pl', 'ca', 'nl', 'ar', 'sv', 'it', 'id', 'hi', 'fi', 'vi', 'he', 'uk', 'el', 'ms', 'cs', 'ro', 'da', 'hu', 'ta', 'no', 'th', 'ur', 'hr', 'bg', 'lt', 'la', 'mi', 'ml', 'cy', 'sk', 'te', 'fa', 'lv', 'bn', 'sr', 'az', 'sl', 'kn', 'et', 'mk', 'br', 'eu', 'is', 'hy', 'ne', 'mn', 'bs', 'kk', 'sq', 'sw', 'gl', 'mr', 'pa', 'si', 'km', 'so', 'am', 'yi', 'lo', 'my']
        model.generation_config.lang_to_id = {l: 50259 + i for i, l in enumerate(WHISPER_LANGS)}
    if not hasattr(model.generation_config, 'task_to_id') or not model.generation_config.task_to_id:
        model.generation_config.task_to_id = {'transcribe': 50358, 'translate': 50359}
    if not hasattr(model.generation_config, 'is_multilingual'):
        model.generation_config.is_multilingual = True
    if not hasattr(model.generation_config, 'begin_suppress_tokens') or model.generation_config.begin_suppress_tokens is None:
        model.generation_config.begin_suppress_tokens = []
    if not hasattr(model.generation_config, 'suppress_tokens') or model.generation_config.suppress_tokens is None:
        model.generation_config.suppress_tokens = []
    processor = AutoProcessor.from_pretrained(slot.source, local_files_only=local_only)
    asr_pipeline = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        torch_dtype=dtype,
        device=0 if slot.device == "cuda" else -1,
    )
    adapter = TransformersWhisperAdapter(model=model, pipeline=asr_pipeline, slot=slot)
    adapter._assert_gpu_resident()
    return adapter



class FasterWhisperAdapter:
    """Adapter that wraps faster-whisper's API to look like openai-whisper's API."""

    def __init__(self, model: Any, slot: "WhisperModelSlot") -> None:
        self.model = model
        self.slot = slot

    def transcribe(self, audio_path: str, **kwargs: Any) -> dict[str, Any]:
        from faster_whisper import WhisperModel
        language = kwargs.get("language", "auto")
        if language == "auto":
            language = None
        word_timestamps = bool(kwargs.get("word_timestamps", False))
        beam_size = int(kwargs.get("beam_size", 5))
        if beam_size < 1:
            beam_size = 1
        # faster-whisper params
        fw_kwargs = {
            "language": language,
            "task": "transcribe",
            "beam_size": beam_size,
            "word_timestamps": word_timestamps,
            "vad_filter": False,
        }
        # Optional settings the runner passes
        if "best_of" in kwargs:
            fw_kwargs["best_of"] = max(1, int(kwargs["best_of"]))
        if "temperature" in kwargs:
            fw_kwargs["temperature"] = float(kwargs["temperature"])
        if "initial_prompt" in kwargs:
            fw_kwargs["initial_prompt"] = kwargs["initial_prompt"]
        if "condition_on_previous_text" in kwargs:
            fw_kwargs["condition_on_previous_text"] = bool(kwargs["condition_on_previous_text"])

        segments_iter, info = self.model.transcribe(audio_path, **fw_kwargs)
        segments_list: list[dict[str, Any]] = []
        full_text_parts: list[str] = []
        for seg in segments_iter:
            seg_text = seg.text or ""
            seg_text = seg_text.strip()
            if not seg_text:
                continue
            seg_dict: dict[str, Any] = {
                "id": len(segments_list),
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg_text,
            }
            words_list: list[dict[str, Any]] = []
            if word_timestamps and getattr(seg, "words", None):
                for w in seg.words:
                    if w.word is None or getattr(w, "start", None) is None:
                        continue
                    words_list.append({
                        "start": float(w.start),
                        "end": float(getattr(w, "end", w.start)),
                        "word": str(w.word),
                        "probability": float(getattr(w, "probability", 1.0) or 1.0),
                    })
            if words_list:
                seg_dict["words"] = words_list
            segments_list.append(seg_dict)
            full_text_parts.append(seg_text)
        return {
            "text": " ".join(full_text_parts).strip(),
            "segments": segments_list,
            "language": info.language or "en",
        }

    def cpu(self) -> None:
        # faster-whisper manages device internally; this is a no-op
        return None

    def to(self, device: str) -> "FasterWhisperAdapter":
        # Device switching is a no-op (faster-whisper holds GPU mem in CTranslate2)
        return self


def _load_faster_whisper(slot: "WhisperModelSlot", download_root: "Path") -> "FasterWhisperAdapter":
    """Load a faster-whisper model. auto-downloads from HuggingFace if not local.

    slot.source can be:
      - a local CTranslate2 model directory path, or
      - a HuggingFace repo id like "deepdml/faster-whisper-large-v3" or "Systran/faster-whisper-large-v3"
    slot.dtype maps to CTranslate2 compute_type:
      float16 -> "float16" (or "int8_float16" if VRAM tight)
      int8 -> "int8"
    """
    from faster_whisper import WhisperModel

    dtype_lower = (slot.dtype or "float16").lower()
    # Map to CTranslate2 compute_type
    if dtype_lower in ("int8", "int8_float16", "int8_float32"):
        compute_type = "int8"
    elif dtype_lower in ("bfloat16",):
        compute_type = "bfloat16"  # ctranslate2 supports bfloat16 on some hw
    else:
        # float16 default. For large-v3 on 6GB GPU prefer int8 to fit comfortably.
        # If user explicitly set int8 in slot dtype use that, else use int8 if model >= large.
        compute_type = "float16"
    src = slot.source
    if not src:
        raise RuntimeError(f"faster_whisper slot {slot.slot_id}: source is empty")
    # Heuristic: if source doesn't look like a HF repo and not a local dir, treat as name
    if not os.path.isabs(src) and "/" not in src:
        # bare model name like "large-v3" -> map to HF faster-whisper
        from .whisper_backends import _FASTER_WHISPER_NAME_MAP  # type: ignore
        src = _FASTER_WHISPER_NAME_MAP.get(src, f"Systran/faster-whisper-{src}")
    device = "cuda" if slot.device == "cuda" else "cpu"
    model = WhisperModel(
        src,
        device=device,
        compute_type=compute_type,
        download_root=str(download_root) if download_root else None,
    )
    return FasterWhisperAdapter(model=model, slot=slot)


# Mapping for bare names -> HuggingFace repo ids (faster-whisper-format models).
_FASTER_WHISPER_NAME_MAP = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "turbo": "deepdml/faster-whisper-large-v3-turbo",
}

def load_whisper_backend(slot: WhisperModelSlot, *, download_root: Path, log: LogCallback) -> Any:
    log(
        "[AutoSu] loading whisper slot="
        f"{slot.slot_id} backend={slot.backend} device={slot.device} fingerprint={slot.fingerprint[:12]}"
    )
    if slot.backend == "openai_whisper":
        return _load_openai_whisper(slot, download_root)
    if slot.backend == "hf_transformers":
        return _load_transformers_whisper(slot)
    if slot.backend == "faster_whisper":
        return _load_faster_whisper(slot, download_root)
    raise RuntimeError(f"unsupported Whisper backend: {slot.backend}")
