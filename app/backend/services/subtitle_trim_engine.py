"""Subtitle Trim Silence engine — port of Tk ab_roll_processor._trim_a_silence_proxy.

Used by autosu_runner.run_autosu_on_outputs to trim leading/trailing/middle
silence from input videos BEFORE subtitle burn-in. SRT cues are re-aligned
to the trimmed timeline so subtitle timing remains correct.

Algorithm (mirror of ab_roll_processor.py:275-440):
  1. ffmpeg silencedetect=noise=XdB:d=Y  → parse intervals
  2. _keep_intervals_from_silence(duration, silences, margin_sec, min_keep_sec)
  3. ffmpeg -filter_complex trim/atrim + setpts/asetpts + concat
  4. re-align cues by subtracting cumulative silence duration
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

LogCallback = Callable[[str], None]


def _clamped_float(value: object, default: float, *, min_value: float, max_value: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = float(default)
    return max(min_value, min(max_value, number))


def _parse_silencedetect_intervals(stderr: str, duration: float) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    current_start: float | None = None
    for line in str(stderr or "").splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            try:
                current_start = max(0.0, float(start_match.group(1)))
            except ValueError:
                current_start = None
            continue
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if end_match and current_start is not None:
            try:
                end = min(max(0.0, float(end_match.group(1))), duration)
            except ValueError:
                current_start = None
                continue
            if end > current_start:
                intervals.append((current_start, end))
            current_start = None
    if current_start is not None and duration > current_start:
        intervals.append((current_start, duration))
    return intervals


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _keep_intervals_from_silence(
    duration: float,
    silences: list[tuple[float, float]],
    *,
    margin_sec: float,
    min_keep_sec: float,
) -> list[tuple[float, float]]:
    cut_intervals: list[tuple[float, float]] = []
    for start, end in silences:
        cut_start = max(0.0, start + margin_sec)
        cut_end = min(duration, end - margin_sec)
        if cut_end > cut_start:
            cut_intervals.append((cut_start, cut_end))
    cut_intervals = _merge_intervals(cut_intervals)

    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cut_intervals:
        if start - cursor >= min_keep_sec:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= min_keep_sec:
        keep.append((cursor, duration))
    return keep


def _probe_duration(src: Path, ffmpeg_exe: str) -> float:
    """ffprobe duration, fallback to ffmpeg parse if ffprobe missing."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(src),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            duration = float((data.get("format") or {}).get("duration") or 0.0)
            if duration > 0:
                return duration
    except Exception:
        pass
    # Fallback: parse Duration from ffmpeg stderr
    try:
        cmd = [ffmpeg_exe, "-hide_banner", "-i", str(src)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr or "")
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


def _run_ffmpeg(cmd: list[str], timeout_sec: float = 3600.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout_sec,
    )


def re_align_cues(
    cues: list[tuple[float, float, str]],
    silences: list[tuple[float, float]],
    *,
    margin_sec: float = 0.0,
) -> list[tuple[float, float, str]]:
    """Shift cues by subtracting cumulative silence duration BEFORE each cue start.

    Each cue's new start = original start - (sum of cut intervals that overlap
    or come before it). Cues that span across a silence get compressed too.
    """
    if not silences or not cues:
        return list(cues)
    cut_intervals: list[tuple[float, float]] = []
    for s_start, s_end in silences:
        cs = max(0.0, s_start + margin_sec)
        ce = max(cs, s_end - margin_sec)
        if ce > cs:
            cut_intervals.append((cs, ce))
    cut_intervals = _merge_intervals(cut_intervals)
    if not cut_intervals:
        return list(cues)
    out: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        shift = 0.0
        for cs, ce in cut_intervals:
            if ce <= start:
                # silence is entirely before this cue — full shift
                shift += (ce - cs)
            elif cs < end and ce > start:
                # overlap — count only the part of silence that is before the cue's start
                shift += max(0.0, min(start, ce) - cs)
            # else: silence is after this cue entirely
        new_start = max(0.0, start - shift)
        new_end = max(new_start, end - shift)
        out.append((new_start, new_end, text))
    return out


def trim_silence_proxy(
    src: Path,
    work_dir: Path,
    trim_settings: Any,
    log: LogCallback,
    *,
    ffmpeg_exe: str,
) -> tuple[Path, dict[str, Any]]:
    """Trim silence from a video, return (output_path, manifest).

    If trim_silence is disabled, returns (src, {"skipped": True, ...}).
    """
    manifest: dict[str, Any] = {
        "source": str(src),
        "output": str(src),
        "skipped": True,
        "reason": "not_requested",
    }
    if not bool(getattr(trim_settings, "trim_silence", False)):
        return src, manifest

    work_dir.mkdir(parents=True, exist_ok=True)
    duration = max(float(_probe_duration(src, ffmpeg_exe) or 0.0), 0.0)
    if duration <= 0:
        log(f"[SUBTITLE_TRIM] skip {src.name}: duration unavailable")
        manifest["reason"] = "duration_unavailable"
        return src, manifest

    threshold_db = _clamped_float(
        getattr(trim_settings, "trim_silence_threshold_db", -40.0),
        -40.0, min_value=-90.0, max_value=0.0,
    )
    min_silence = _clamped_float(
        getattr(trim_settings, "trim_silence_min_silence_sec", 0.5),
        0.5, min_value=0.05, max_value=30.0,
    )
    margin_sec = _clamped_float(
        getattr(trim_settings, "trim_silence_margin_sec", 0.0),
        0.0, min_value=0.0, max_value=5.0,
    )
    min_keep_sec = _clamped_float(
        getattr(trim_settings, "trim_silence_min_keep_sec", 0.08),
        0.08, min_value=0.0, max_value=5.0,
    )
    min_output_sec = _clamped_float(
        getattr(trim_settings, "trim_silence_min_output_sec", 1.0),
        1.0, min_value=0.0, max_value=60.0,
    )

    detect_cmd = [
        ffmpeg_exe, "-hide_banner", "-nostats", "-i", str(src),
        "-af", f"silencedetect=noise={threshold_db:.1f}dB:d={min_silence:.3f}",
        "-f", "null", "-",
    ]
    try:
        detect = _run_ffmpeg(detect_cmd, timeout_sec=600.0)
    except Exception as exc:
        log(f"[SUBTITLE_TRIM] skip {src.name}: silencedetect exception: {exc}")
        manifest["reason"] = "detect_exception"
        return src, manifest
    if detect.returncode != 0:
        tail = (detect.stdout or "")[-300:].replace("\n", " ")
        log(f"[SUBTITLE_TRIM] skip {src.name}: silencedetect failed {tail}")
        manifest["reason"] = "detect_failed"
        return src, manifest

    silences = _parse_silencedetect_intervals(detect.stdout or "", duration)
    if not silences:
        log(f"[SUBTITLE_TRIM] no silence detected {src.name} duration={duration:.3f}s")
        manifest["reason"] = "no_silence"
        return src, manifest

    keep = _keep_intervals_from_silence(
        duration, silences,
        margin_sec=margin_sec, min_keep_sec=min_keep_sec,
    )
    kept_duration = sum(end - start for start, end in keep)
    removed_duration = max(0.0, duration - kept_duration)
    if kept_duration < min_output_sec or removed_duration < 0.05:
        log(
            f"[SUBTITLE_TRIM] skip {src.name}: kept={kept_duration:.3f}s "
            f"removed={removed_duration:.3f}s min_output={min_output_sec:.3f}s"
        )
        manifest["reason"] = "min_output_or_remove_too_small"
        manifest["kept_duration"] = kept_duration
        manifest["removed_duration"] = removed_duration
        return src, manifest

    if len(keep) > 200:
        log(f"[SUBTITLE_TRIM] skip {src.name}: keep intervals {len(keep)} > 200")
        manifest["reason"] = "too_many_intervals"
        return src, manifest

    out = work_dir / f"{src.stem}.autosu_trimmed.mp4"
    tmp_out = out.with_name(f"{out.stem}__tmp{out.suffix}")
    tmp_out.unlink(missing_ok=True)

    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    for idx, (start, end) in enumerate(keep):
        filter_parts.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{idx}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{idx}]"
        )
        concat_inputs.append(f"[v{idx}][a{idx}]")
    filter_parts.append(
        "".join(concat_inputs) + f"concat=n={len(keep)}:v=1:a=1[vout][aout]"
    )
    filter_complex = ";".join(filter_parts)
    if len(filter_complex) > 80000:
        log(f"[SUBTITLE_TRIM] skip {src.name}: filter graph too long ({len(filter_complex)} chars)")
        manifest["reason"] = "filter_too_long"
        return src, manifest

    cmd = [
        ffmpeg_exe, "-y", "-i", str(src),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(tmp_out),
    ]
    try:
        result = _run_ffmpeg(cmd, timeout_sec=3600.0)
    except subprocess.TimeoutExpired:
        log(f"[SUBTITLE_TRIM] ffmpeg timeout {src.name}")
        tmp_out.unlink(missing_ok=True)
        manifest["reason"] = "ffmpeg_timeout"
        return src, manifest
    except Exception as exc:
        log(f"[SUBTITLE_TRIM] ffmpeg exception {src.name}: {exc}")
        tmp_out.unlink(missing_ok=True)
        manifest["reason"] = "ffmpeg_exception"
        return src, manifest
    if result.returncode != 0 or not tmp_out.exists():
        tmp_out.unlink(missing_ok=True)
        tail = (result.stdout or "")[-500:].replace("\n", " ")
        log(f"[SUBTITLE_TRIM] ffmpeg failed {src.name}: {tail}")
        manifest["reason"] = "ffmpeg_failed"
        return src, manifest
    tmp_out.replace(out)
    new_duration = max(float(_probe_duration(out, ffmpeg_exe) or 0.0), 0.0)
    manifest = {
        "source": str(src),
        "output": str(out),
        "skipped": False,
        "before_duration": duration,
        "after_duration": new_duration,
        "planned_after_duration": kept_duration,
        "removed_duration": max(0.0, duration - new_duration),
        "threshold_db": threshold_db,
        "min_silence_sec": min_silence,
        "margin_sec": margin_sec,
        "silence_intervals": silences,
        "keep_intervals": keep,
    }
    try:
        manifest_path = work_dir / "subtitle_trim_manifest.jsonl"
        with manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, ensure_ascii=False) + "\n")
    except Exception as exc:
        log(f"[SUBTITLE_TRIM][WARN] manifest write failed: {exc}")
    log(
        f"[SUBTITLE_TRIM] applied {src.name}: before={duration:.3f}s "
        f"after={new_duration:.3f}s removed={duration - new_duration:.3f}s "
        f"intervals={len(silences)}"
    )
    return out, manifest
