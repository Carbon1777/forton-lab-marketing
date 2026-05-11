"""Tests for video_compositor — ffmpeg primitives with synthetic fixtures.

Phase 11-04. All ffprobe-based assertions are gated on a shutil.which check so
the suite still collects when ffmpeg is absent. Local dev box (RESEARCH §line 84)
has ffmpeg 8.1 installed so every test runs there.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
FIX_DIR = REPO / "tests" / "fixtures"
RED_PNG = FIX_DIR / "synthetic_1080x1920_red.png"
SILENCE_WAV = FIX_DIR / "synthetic_silence_2s.wav"

ffmpeg_present = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _ffprobe(path: Path) -> dict:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(r.stdout)


# ---------------------------------------------------------------------------
# Pure-Python helpers (no ffmpeg required)
# ---------------------------------------------------------------------------


def test_rgb_to_ass_bgr_brand_gold():
    from src.ai_talent.video_compositor import rgb_to_ass_bgr

    assert rgb_to_ass_bgr("#F4C757") == "&H0057C7F4"


def test_rgb_to_ass_bgr_brand_dark():
    from src.ai_talent.video_compositor import rgb_to_ass_bgr

    assert rgb_to_ass_bgr("#1A0F08") == "&H00080F1A"


def test_rgb_to_ass_bgr_accepts_with_and_without_hash():
    from src.ai_talent.video_compositor import rgb_to_ass_bgr

    assert rgb_to_ass_bgr("F4C757") == rgb_to_ass_bgr("#F4C757")


def test_rgb_to_ass_bgr_rejects_invalid():
    from src.ai_talent.video_compositor import CompositorError, rgb_to_ass_bgr

    with pytest.raises(CompositorError):
        rgb_to_ass_bgr("nothex!")


# ---------------------------------------------------------------------------
# _require_ffmpeg gate
# ---------------------------------------------------------------------------


@ffmpeg_present
def test_require_ffmpeg_passes():
    from src.ai_talent.video_compositor import _require_ffmpeg

    _require_ffmpeg()  # should not raise


# ---------------------------------------------------------------------------
# ken_burns
# ---------------------------------------------------------------------------


@ffmpeg_present
def test_ken_burns_emits_1080x1920_25fps(tmp_path: Path):
    from src.ai_talent.video_compositor import ken_burns

    out = tmp_path / "kb.mp4"
    ken_burns(RED_PNG, duration_sec=2.0, out=out)
    assert out.exists() and out.stat().st_size > 0
    info = _ffprobe(out)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert vs["width"] == 1080
    assert vs["height"] == 1920
    assert vs["r_frame_rate"] == "25/1"
    assert vs["codec_name"] == "h264"
    dur = float(info["format"]["duration"])
    assert 1.9 <= dur <= 2.1, f"unexpected duration {dur}"


@ffmpeg_present
def test_ken_burns_no_audio_track(tmp_path: Path):
    from src.ai_talent.video_compositor import ken_burns

    out = tmp_path / "kb.mp4"
    ken_burns(RED_PNG, duration_sec=1.0, out=out)
    info = _ffprobe(out)
    audio_streams = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert audio_streams == []


# ---------------------------------------------------------------------------
# concat_segments
# ---------------------------------------------------------------------------


@ffmpeg_present
def test_concat_segments_two_clips(tmp_path: Path):
    from src.ai_talent.video_compositor import concat_segments, ken_burns

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    ken_burns(RED_PNG, 1.0, a)
    ken_burns(RED_PNG, 1.0, b)
    out = tmp_path / "ab.mp4"
    concat_segments([a, b], out)
    info = _ffprobe(out)
    dur = float(info["format"]["duration"])
    assert 1.8 <= dur <= 2.2, f"concat duration {dur} not ~2.0"


def test_concat_segments_empty_raises():
    from src.ai_talent.video_compositor import CompositorError, concat_segments

    with pytest.raises(CompositorError):
        concat_segments([], Path("/tmp/wont-exist.mp4"))


# ---------------------------------------------------------------------------
# mux_audio
# ---------------------------------------------------------------------------


@ffmpeg_present
def test_mux_audio_adds_track(tmp_path: Path):
    from src.ai_talent.video_compositor import ken_burns, mux_audio

    v = tmp_path / "v.mp4"
    ken_burns(RED_PNG, 2.0, v)
    out = tmp_path / "v_with_audio.mp4"
    mux_audio(v, [SILENCE_WAV], out)
    info = _ffprobe(out)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["codec_name"] == "aac"


@ffmpeg_present
def test_mux_audio_shortest_applied(tmp_path: Path):
    """Video 1s + audio 2s should produce ≤1.15s output (-shortest)."""
    from src.ai_talent.video_compositor import ken_burns, mux_audio

    v = tmp_path / "v_short.mp4"
    ken_burns(RED_PNG, 1.0, v)
    out = tmp_path / "muxed.mp4"
    mux_audio(v, [SILENCE_WAV], out)
    info = _ffprobe(out)
    dur = float(info["format"]["duration"])
    assert dur <= 1.15, f"shortest not applied: dur={dur}"


def test_mux_audio_empty_voice_raises(tmp_path: Path):
    from src.ai_talent.video_compositor import CompositorError, mux_audio

    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    with pytest.raises(CompositorError):
        mux_audio(v, [], tmp_path / "out.mp4")


# ---------------------------------------------------------------------------
# burn_subtitles — command-shape only (font dependency makes execution fragile)
# ---------------------------------------------------------------------------


def test_burn_subtitles_command_shape(tmp_path: Path):
    """Verify -vf subtitles=... is constructed without running the filter."""
    from src.ai_talent import video_compositor

    srt = tmp_path / "captions.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
    )
    v = tmp_path / "v.mp4"
    v.write_bytes(b"fake_mp4")

    with patch.object(video_compositor.subprocess, "run") as run_mock:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        video_compositor.burn_subtitles(v, srt, tmp_path / "out.mp4")

    call_args = run_mock.call_args[0][0]
    vf_index = call_args.index("-vf")
    vf_arg = call_args[vf_index + 1]
    assert "subtitles=" in vf_arg
    # Brand-gold primary colour in BGR form
    assert "57C7F4" in vf_arg
    # Burn flags must enforce libx264 + yuv420p + faststart
    assert "libx264" in call_args
    assert "+faststart" in call_args
