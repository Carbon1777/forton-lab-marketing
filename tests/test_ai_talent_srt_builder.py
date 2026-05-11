"""Phase 11 Plan 05 — srt_builder.py (Stage 5) tests.

Coverage:
    - _srt_ts: format HH:MM:SS,mmm with various magnitudes
    - build_srt: Option A (character alignment) + Option C (fallback approx)
    - mixed inputs (one fallback line + one alignment line)
    - 1-based monotonic numbering
    - empty/length-mismatch error paths
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ai_talent.srt_builder import SrtBuilderError, _srt_ts, build_srt


# --------------------------------------------------------------------------
# _srt_ts
# --------------------------------------------------------------------------


def test_srt_ts_zero():
    assert _srt_ts(0.0) == "00:00:00,000"


def test_srt_ts_subsecond():
    assert _srt_ts(0.5) == "00:00:00,500"


def test_srt_ts_seconds_only():
    assert _srt_ts(1.5) == "00:00:01,500"


def test_srt_ts_minutes_hours():
    # 1h 1m 1.123s
    assert _srt_ts(3661.123) == "01:01:01,123"


def test_srt_ts_negative_clamps_to_zero():
    assert _srt_ts(-1.0) == "00:00:00,000"


def test_srt_ts_minutes_rollover():
    assert _srt_ts(60.0) == "00:01:00,000"


# --------------------------------------------------------------------------
# build_srt — Option A (character alignment)
# --------------------------------------------------------------------------


def _write_alignment_ts(path: Path, chars, starts, ends):
    path.write_text(json.dumps({
        "fallback": False,
        "characters": chars,
        "starts": starts,
        "ends": ends,
    }), encoding="utf-8")


def test_build_srt_from_alignment_single_sentence(tmp_path):
    ts = tmp_path / "line1.timestamps.json"
    _write_alignment_ts(
        ts,
        ["П", "р", "и", "в", "е", "т", "."],
        [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
    )
    out = tmp_path / "out.srt"
    build_srt([ts], ["Привет."], out)
    body = out.read_text(encoding="utf-8")
    # Numbered 1, with HH:MM:SS,mmm format
    assert body.startswith("1\n")
    assert "00:00:00,000 --> 00:00:00,700" in body
    assert "Привет." in body


def test_build_srt_from_alignment_multi_sentence_splits_on_period(tmp_path):
    """Two sentences in one alignment payload -> two SRT entries."""
    ts = tmp_path / "x.timestamps.json"
    text = "А.Б."
    chars = list(text)
    starts = [0.0, 0.5, 1.0, 1.5]
    ends   = [0.5, 1.0, 1.5, 2.0]
    _write_alignment_ts(ts, chars, starts, ends)
    out = tmp_path / "out.srt"
    build_srt([ts], [text], out)
    body = out.read_text(encoding="utf-8")
    assert "1\n" in body
    assert "2\n" in body


# --------------------------------------------------------------------------
# build_srt — Option C (fallback / approximation)
# --------------------------------------------------------------------------


def _write_fallback_ts(path: Path, text: str):
    path.write_text(json.dumps({"fallback": True, "text": text}),
                    encoding="utf-8")


def test_build_srt_punctuation_fallback_two_sentences(tmp_path):
    ts = tmp_path / "line1.timestamps.json"
    _write_fallback_ts(ts, "Hello. World.")
    out = tmp_path / "out.srt"
    build_srt([ts], ["Hello. World."], out, audio_durations_sec=[2.0])
    body = out.read_text(encoding="utf-8")
    assert body.startswith("1\n")
    # Two entries
    assert "\n2\n" in body
    assert "Hello." in body
    assert "World." in body


def test_build_srt_fallback_uses_audio_duration(tmp_path):
    ts = tmp_path / "x.json"
    _write_fallback_ts(ts, "One. Two.")
    out = tmp_path / "out.srt"
    build_srt([ts], ["One. Two."], out, audio_durations_sec=[4.0])
    body = out.read_text(encoding="utf-8")
    # Total span ~4 seconds — last entry's end timestamp ~= 4.000
    assert "00:00:04,000" in body or "00:00:03,9" in body


def test_build_srt_fallback_handles_empty_text(tmp_path):
    ts = tmp_path / "x.json"
    _write_fallback_ts(ts, "")
    out = tmp_path / "out.srt"
    build_srt([ts], [""], out, audio_durations_sec=[1.0])
    # Empty text -> empty SRT body (or just trailing newline)
    body = out.read_text(encoding="utf-8")
    assert body.strip() == ""


# --------------------------------------------------------------------------
# Mixed inputs
# --------------------------------------------------------------------------


def test_build_srt_mixed_alignment_and_fallback(tmp_path):
    a = tmp_path / "a.json"
    _write_alignment_ts(a, ["X", "."], [0.0, 0.5], [0.5, 1.0])
    b = tmp_path / "b.json"
    _write_fallback_ts(b, "Sec.")
    out = tmp_path / "out.srt"
    build_srt(
        [a, b],
        ["X.", "Sec."],
        out,
        line_offsets_sec=[0.0, 1.0],
        audio_durations_sec=[1.0, 1.0],
    )
    body = out.read_text(encoding="utf-8")
    # Two entries minimum, monotonic numbering
    lines = body.splitlines()
    indices = [ln for ln in lines if ln.isdigit()]
    assert indices == sorted(indices, key=int)
    # Second line offset visible
    assert "00:00:01," in body  # any timestamp in 1.x range


def test_build_srt_emits_monotonic_numbering(tmp_path):
    ts = tmp_path / "x.json"
    _write_fallback_ts(ts, "A. B. C.")
    out = tmp_path / "out.srt"
    build_srt([ts], ["A. B. C."], out, audio_durations_sec=[3.0])
    indices = [ln for ln in out.read_text().splitlines() if ln.isdigit()]
    assert indices == ["1", "2", "3"]


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------


def test_build_srt_length_mismatch_raises(tmp_path):
    ts = tmp_path / "x.json"
    _write_fallback_ts(ts, "X.")
    with pytest.raises(SrtBuilderError, match="!="):
        build_srt([ts], ["X.", "Y."], tmp_path / "o.srt")


def test_build_srt_missing_timestamps_file_raises(tmp_path):
    with pytest.raises(SrtBuilderError, match="missing"):
        build_srt([tmp_path / "nope.json"], ["x"], tmp_path / "o.srt")


def test_build_srt_alignment_length_mismatch_raises(tmp_path):
    ts = tmp_path / "x.json"
    # characters longer than starts/ends -> error
    ts.write_text(json.dumps({
        "fallback": False,
        "characters": ["A", "B"],
        "starts": [0.0],
        "ends": [0.5],
    }), encoding="utf-8")
    with pytest.raises(SrtBuilderError, match="length mismatch"):
        build_srt([ts], ["AB"], tmp_path / "o.srt")
