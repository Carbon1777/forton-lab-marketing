"""2-pass libx264 encoder fitting to a target file-size ceiling.

Phase 11 requirement: final mp4 ≤ 18 МБ for the Дзен upload path
(PROJECT.md / PUBLISHING_RULES — Дзен caps at 20 МБ via the TG-crosspost route;
we target 18 МБ to leave muxer-overhead headroom).

Single-pass CRF overshoots target by 10-30% on short clips; 2-pass with a
computed bitrate guarantees ±2% of target. Algorithm:

    bitrate_kbps = (target_mb * 8 * 1024 / duration_sec) - audio_kbps

If the resulting bitrate is below :data:`MIN_BITRATE_KBPS` (400 kbps) libx264
quality is unacceptable — we raise :class:`BitrateError` so the caller can trim
the clip or raise the size budget instead of shipping garbage.

Reused from :mod:`video_compositor`: :func:`_require_ffmpeg`, :class:`CompositorError`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from src.ai_talent.video_compositor import CompositorError, _require_ffmpeg

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent

MIN_BITRATE_KBPS: Final[int] = 400
"""Below this libx264 produces visible artefacts at 1080x1920 25fps."""

SIZE_TOLERANCE_MB: Final[float] = 0.5
"""Muxer-overhead margin; outputs within target+tolerance pass."""


class BitrateError(CompositorError):
    """Raised when target bitrate < MIN_BITRATE_KBPS or output exceeds tolerance."""


def compute_bitrate_kbps(
    target_mb: float,
    duration_sec: float,
    audio_kbps: int = 96,
) -> int:
    """Compute the libx264 ``-b:v`` to hit ``target_mb`` over ``duration_sec``.

    Formula
    -------
    ``bitrate_kbps = (target_mb * 8 * 1024 / duration_sec) - audio_kbps``

    Raises
    ------
    BitrateError
        - ``duration_sec`` ≤ 0
        - resulting bitrate < :data:`MIN_BITRATE_KBPS`
    """
    if duration_sec <= 0:
        raise BitrateError(f"duration_sec must be positive, got {duration_sec}")
    kbps = int((target_mb * 8 * 1024 / duration_sec) - audio_kbps)
    if kbps < MIN_BITRATE_KBPS:
        raise BitrateError(
            f"target {target_mb}MB @ {duration_sec:.1f}s yields {kbps}kbps "
            f"< {MIN_BITRATE_KBPS} — trim length or raise target"
        )
    return kbps


def _probe_duration(video: Path) -> float:
    """Return container duration in seconds via ffprobe (raises if unparseable)."""
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BitrateError(f"ffprobe failed on {video}") from exc
    return float(r.stdout.strip())


def fit_to_size(
    src: Path,
    out: Path,
    target_mb: float = 18.0,
    audio_kbps: int = 96,
) -> Path:
    """2-pass libx264 encode of ``src`` → ``out`` capped at ``target_mb``.

    Pipeline:
      1. ffprobe ``src`` duration
      2. compute target video bitrate from (target_mb, duration, audio_kbps)
      3. Pass 1 (analysis only, ``-an -f mp4 /dev/null``)
      4. Pass 2 (final encode, AAC audio at ``audio_kbps``, +faststart)
      5. Clean up 2-pass log files (``*-0.log`` and ``*-0.log.mbtree``)
      6. Assert output size ≤ target_mb + :data:`SIZE_TOLERANCE_MB`

    Raises
    ------
    BitrateError
        - target bitrate below :data:`MIN_BITRATE_KBPS`
        - ffmpeg subprocess failure
        - output exceeds ``target_mb + SIZE_TOLERANCE_MB``
    """
    _require_ffmpeg()
    duration = _probe_duration(src)
    target_kbps = compute_bitrate_kbps(target_mb, duration, audio_kbps)

    log_prefix = str(src.parent / "_2pass")
    common = [
        "-c:v",
        "libx264",
        "-b:v",
        f"{target_kbps}k",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Pass 1 — analysis only; discard audio, write to /dev/null
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                *common,
                "-pass",
                "1",
                "-passlogfile",
                log_prefix,
                "-an",
                "-f",
                "mp4",
                "/dev/null",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        # Pass 2 — final encode with audio + faststart
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                *common,
                "-pass",
                "2",
                "-passlogfile",
                log_prefix,
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_kbps}k",
                "-movflags",
                "+faststart",
                str(out),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "")[-500:]
        raise BitrateError(f"ffmpeg 2-pass failed: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BitrateError("ffmpeg 2-pass timed out") from exc
    finally:
        for ext in ("-0.log", "-0.log.mbtree"):
            Path(log_prefix + ext).unlink(missing_ok=True)

    actual_mb = out.stat().st_size / (1024 * 1024)
    if actual_mb > target_mb + SIZE_TOLERANCE_MB:
        raise BitrateError(
            f"fit_to_size: output {actual_mb:.2f}MB > "
            f"{target_mb}MB target (tolerance {SIZE_TOLERANCE_MB}MB)"
        )
    return out


__all__ = [
    "BitrateError",
    "MIN_BITRATE_KBPS",
    "SIZE_TOLERANCE_MB",
    "compute_bitrate_kbps",
    "fit_to_size",
]
