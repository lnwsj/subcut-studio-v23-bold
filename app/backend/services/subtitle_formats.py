"""Parse, normalize, and render SRT, WebVTT, and ASS subtitle cues."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_CUES = 5000
MAX_TEXT_LENGTH = 2000
_TIMESTAMP_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})([,.]\d{1,3})?")
_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(86_400.0, float(value)))
    text = str(value or "").strip()
    match = _TIMESTAMP_RE.fullmatch(text)
    if not match:
        try:
            return max(0.0, min(86_400.0, float(text)))
        except (TypeError, ValueError):
            return 0.0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    fraction = float((match.group(4) or ".0").replace(",", "."))
    return min(86_400.0, hours * 3600 + minutes * 60 + seconds + fraction)


def normalize_cues(raw: Any) -> list[dict[str, Any]]:
    """Return validated cues sorted by start time."""
    if not isinstance(raw, list):
        return []
    cues: list[dict[str, Any]] = []
    for index, item in enumerate(raw[:MAX_CUES], start=1):
        if not isinstance(item, dict):
            continue
        start = _seconds(item.get("start"))
        end = _seconds(item.get("end"))
        if end <= start:
            end = min(86_400.0, start + 0.5)
        text = str(item.get("text") or "").replace("\x00", "").strip()[:MAX_TEXT_LENGTH]
        if not text:
            continue
        cues.append({"id": str(item.get("id") or index), "start": round(start, 3), "end": round(end, 3), "text": text})
    cues.sort(key=lambda cue: (cue["start"], cue["end"]))
    for index, cue in enumerate(cues, start=1):
        cue["id"] = str(index)
    return cues


def _parse_range(line: str) -> tuple[float, float] | None:
    if "-->" not in line:
        return None
    left, right = line.split("-->", 1)
    right = right.strip().split()[0] if right.strip() else ""
    start, end = _seconds(left.strip()), _seconds(right)
    return (start, end) if end > start else None


def parse_srt(content: str) -> list[dict[str, Any]]:
    blocks = re.split(r"\r?\n\s*\r?\n", str(content or "").lstrip("\ufeff").strip())
    output: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines()]
        range_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if range_index < 0:
            continue
        timing = _parse_range(lines[range_index])
        text = "\n".join(lines[range_index + 1 :]).strip()
        if timing and text:
            output.append({"start": timing[0], "end": timing[1], "text": text})
    return normalize_cues(output)


def parse_vtt(content: str) -> list[dict[str, Any]]:
    text = str(content or "").lstrip("\ufeff").replace("\r\n", "\n")
    text = re.sub(r"^WEBVTT[^\n]*\n+", "", text, flags=re.IGNORECASE)
    blocks = re.split(r"\n\s*\n", text.strip())
    output: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        range_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if range_index < 0:
            continue
        timing = _parse_range(lines[range_index])
        cue_text = "\n".join(lines[range_index + 1 :]).strip()
        if timing and cue_text:
            output.append({"start": timing[0], "end": timing[1], "text": cue_text})
    return normalize_cues(output)


def parse_ass(content: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for line in str(content or "").lstrip("\ufeff").splitlines():
        if not line.lstrip().lower().startswith("dialogue:"):
            continue
        values = line.split(":", 1)[1].lstrip().split(",", 9)
        if len(values) < 10:
            continue
        start, end = _seconds(values[1]), _seconds(values[2])
        text = _OVERRIDE_RE.sub("", values[9]).replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ").strip()
        if end > start and text:
            output.append({"start": start, "end": end, "text": text})
    return normalize_cues(output)


def detect_format(filename: str, content: str = "") -> str:
    suffix = Path(str(filename or "")).suffix.lower().lstrip(".")
    if suffix in {"srt", "vtt", "ass"}:
        return suffix
    head = str(content or "")[:500].lstrip("\ufeff\n ").lower()
    if head.startswith("webvtt"):
        return "vtt"
    if "[script info]" in head or "dialogue:" in str(content or "").lower():
        return "ass"
    return "srt"


def parse_subtitles(filename: str, content: str) -> tuple[str, list[dict[str, Any]]]:
    fmt = detect_format(filename, content)
    parser = {"srt": parse_srt, "vtt": parse_vtt, "ass": parse_ass}[fmt]
    return fmt, parser(content)


def _srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def _ass_time(seconds: float) -> str:
    centis = int(round(max(0.0, seconds) * 100))
    hours, remainder = divmod(centis, 360_000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def render_subtitles(cues: Any, fmt: str) -> str:
    normalized = normalize_cues(cues)
    kind = str(fmt or "srt").lower()
    if kind == "vtt":
        rows = ["WEBVTT", ""]
        for cue in normalized:
            rows.extend([f"{_vtt_time(cue['start'])} --> {_vtt_time(cue['end'])}", cue["text"], ""])
        return "\n".join(rows).rstrip() + "\n"
    if kind == "ass":
        rows = [
            "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0", "",
            "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,Noto Sans Thai,56,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,90,1", "",
            "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for cue in normalized:
            text = cue["text"].replace("\n", r"\N")
            rows.append(f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(cue['end'])},Default,,0,0,0,,{text}")
        return "\n".join(rows) + "\n"
    rows = []
    for index, cue in enumerate(normalized, start=1):
        rows.extend([str(index), f"{_srt_time(cue['start'])} --> {_srt_time(cue['end'])}", cue["text"], ""])
    return "\n".join(rows).rstrip() + ("\n" if rows else "")
