"""Tests for bitrate_fitter — 2-pass libx264 ≤target_mb."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RED_PNG = REPO / "tests" / "fixtures" / "synthetic_1080x1920_red.png"

ffmpeg_present = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


# ---------------------------------------------------------------------------
# Pure-Python — compute_bitrate_kbps
# ---------------------------------------------------------------------------


def test_compute_bitrate_30s_18mb_96kbps():
    """(18 * 8 * 1024) / 30 - 96 = 4823.2 → int(4823)."""
    from src.ai_talent.bitrate_fitter import compute_bitrate_kbps

    kbps = compute_bitrate_kbps(18.0, 30.0, 96)
    assert 4800 <= kbps <= 4830, f"unexpected kbps {kbps}"


def test_compute_bitrate_too_low_raises():
    from src.ai_talent.bitrate_fitter import BitrateError, compute_bitrate_kbps

    # 18MB over 600s → tiny bitrate, below MIN_BITRATE_KBPS=400.
    with pytest.raises(BitrateError, match="(?i)trim|target"):
        compute_bitrate_kbps(18.0, 600.0, 96)


def test_compute_bitrate_zero_duration_raises():
    from src.ai_talent.bitrate_fitter import BitrateError, compute_bitrate_kbps

    with pytest.raises(BitrateError):
        compute_bitrate_kbps(18.0, 0, 96)


def test_compute_bitrate_negative_duration_raises():
    from src.ai_talent.bitrate_fitter import BitrateError, compute_bitrate_kbps

    with pytest.raises(BitrateError):
        compute_bitrate_kbps(18.0, -5, 96)


# ---------------------------------------------------------------------------
# ffmpeg-dependent — fit_to_size end-to-end
# ---------------------------------------------------------------------------


@ffmpeg_present
def test_fit_to_size_emits_under_target(tmp_path: Path):
    """2-pass encode of a 5-sec Ken Burns clip must land ≤ target_mb + tolerance."""
    from src.ai_talent.bitrate_fitter import fit_to_size
    from src.ai_talent.video_compositor import ken_burns

    src = tmp_path / "src.mp4"
    ken_burns(RED_PNG, 5.0, src)
    out = tmp_path / "fit.mp4"
    fit_to_size(src, out, target_mb=2.0)
    size_mb = out.stat().st_size / (1024 * 1024)
    assert size_mb <= 2.5, f"output {size_mb}MB exceeded target+tolerance"


@ffmpeg_present
def test_fit_to_size_faststart_applied(tmp_path: Path):
    """+faststart relocates moov before mdat. Smoke-check ftyp atom at offset 4."""
    from src.ai_talent.bitrate_fitter import fit_to_size
    from src.ai_talent.video_compositor import ken_burns

    src = tmp_path / "src.mp4"
    ken_burns(RED_PNG, 3.0, src)
    out = tmp_path / "fit.mp4"
    fit_to_size(src, out, target_mb=2.0)
    head = out.read_bytes()[:512]
    assert head[4:8] == b"ftyp", "missing ftyp at start (mp4 not well-formed)"


@ffmpeg_present
def test_fit_to_size_logs_cleaned_up(tmp_path: Path):
    """2-pass log files (*-0.log, *-0.log.mbtree) must not leak."""
    from src.ai_talent.bitrate_fitter import fit_to_size
    from src.ai_talent.video_compositor import ken_burns

    src = tmp_path / "src.mp4"
    ken_burns(RED_PNG, 3.0, src)
    out = tmp_path / "fit.mp4"
    fit_to_size(src, out, target_mb=2.0)
    leftover = list(tmp_path.glob("_2pass*"))
    assert leftover == [], f"2-pass log files leaked: {leftover}"
