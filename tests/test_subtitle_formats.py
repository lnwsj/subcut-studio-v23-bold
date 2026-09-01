"""Unit tests for Subtitle Editor Lite format conversion."""

from app.backend.services.subtitle_formats import (
    normalize_cues,
    parse_ass,
    parse_srt,
    parse_vtt,
    render_subtitles,
)


SAMPLE_CUES = [
    {"start": 0.5, "end": 2.75, "text": "สวัสดีจาก SJ88"},
    {"start": 4.0, "end": 7.25, "text": "บรรทัดแรก\nบรรทัดที่สอง"},
]


def test_srt_round_trip() -> None:
    rendered = render_subtitles(SAMPLE_CUES, "srt")
    parsed = parse_srt(rendered)
    assert [cue["text"] for cue in parsed] == [cue["text"] for cue in SAMPLE_CUES]
    assert parsed[0]["start"] == 0.5
    assert parsed[1]["end"] == 7.25


def test_vtt_round_trip() -> None:
    rendered = render_subtitles(SAMPLE_CUES, "vtt")
    assert rendered.startswith("WEBVTT")
    parsed = parse_vtt(rendered)
    assert len(parsed) == 2
    assert parsed[1]["text"] == "บรรทัดแรก\nบรรทัดที่สอง"


def test_ass_round_trip_and_override_removal() -> None:
    rendered = render_subtitles(SAMPLE_CUES, "ass")
    parsed = parse_ass(rendered.replace("สวัสดีจาก SJ88", r"{\\b1}สวัสดีจาก SJ88"))
    assert len(parsed) == 2
    assert parsed[0]["text"] == "สวัสดีจาก SJ88"
    assert parsed[1]["text"] == "บรรทัดแรก\nบรรทัดที่สอง"


def test_normalize_repairs_ranges_and_sorts() -> None:
    normalized = normalize_cues([
        {"start": 5, "end": 4, "text": "ท้าย"},
        {"start": -2, "end": 1, "text": "ต้น"},
        {"start": 1, "end": 2, "text": ""},
    ])
    assert [cue["text"] for cue in normalized] == ["ต้น", "ท้าย"]
    assert normalized[1]["end"] > normalized[1]["start"]
