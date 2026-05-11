"""ffmpeg primitives for the AI-talent video pipeline.

Five subprocess-based operations + an RGB→ASS-BGR colour converter:
    ken_burns(image, duration, out)            — static frame → 1080x1920 9:16 mp4
    concat_segments([seg_a, seg_b, ...], out)  — concat demuxer, -c copy
    mux_audio(video, voice_mp3s, out)          — overlay audio track(s), -shortest
    burn_subtitles(video, srt, out, ...)       — burned-in subtitles via subtitles= filter
    rgb_to_ass_bgr("#F4C757") -> "&H0057C7F4"  — Pitfall 2 mitigation

All produce 1080x1920 @ 25fps libx264 yuv420p mp4 with -movflags +faststart so
the result satisfies PUBLISHING_RULES §3 (TG/VK/Дзен streaming requirement).

Each entry-point calls _require_ffmpeg() first and raises CompositorError on
missing binary — Phase 11 hard-requires ffmpeg/ffprobe (unlike tg_post.py which
gracefully degrades).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_FPS: Final[int] = 25
DEFAULT_RESOLUTION_WH: Final[tuple[int, int]] = (1080, 1920)


class CompositorError(RuntimeError):
    """Raised on missing ffmpeg/ffprobe or ffmpeg subprocess failure."""


def _require_ffmpeg() -> None:
    """Hard gate. Phase 11 cannot proceed without both binaries on PATH."""
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not installed (brew install ffmpeg)")
    if shutil.which("ffprobe") is None:
        raise CompositorError("ffprobe not installed (brew install ffmpeg)")


def _run(args: list[str], *, timeout: int = 300) -> None:
    """Run `ffmpeg -y <args>` capturing stderr; raise CompositorError on fail.

    timeout default 300s (T-11-04-02 DoS mitigation per threat model).
    """
    try:
        subprocess.run(
            ["ffmpeg", "-y", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "")[-500:]
        raise CompositorError(
            f"ffmpeg failed (exit {exc.returncode}): {tail}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CompositorError(f"ffmpeg timed out after {timeout}s") from exc


def rgb_to_ass_bgr(hex_rgb: str) -> str:
    """Convert ``#RRGGBB`` → ASS-spec ``&H00BBGGRR`` (BGR byte order).

    Pitfall 2 (11-RESEARCH.md): ASS subtitle ``force_style`` takes its
    PrimaryColour/OutlineColour as BGR, not RGB. Naive hex copy produces
    a wrong (synth-green) colour instead of brand gold.

    Examples
    --------
    >>> rgb_to_ass_bgr("#F4C757")
    '&H0057C7F4'
    >>> rgb_to_ass_bgr("#1A0F08")
    '&H00080F1A'
    """
    h = hex_rgb.lstrip("#").upper()
    if len(h) != 6 or not all(c in "0123456789ABCDEF" for c in h):
        raise CompositorError(f"invalid hex color {hex_rgb!r}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}"


def ken_burns(
    image: Path,
    duration_sec: float,
    out: Path,
    fps: int = DEFAULT_FPS,
) -> Path:
    """Slow zoom-in (1.0 → 1.08) over a still image → 1080x1920 mp4.

    Output: libx264 yuv420p, no audio, +faststart, exactly ``fps`` r_frame_rate.

    Notes
    -----
    The ``zoompan`` filter defaults to 1x1 output if ``s=`` is omitted — we
    force ``s=1080x1920`` explicitly. ``d=`` is in frames, not seconds.
    """
    _require_ffmpeg()
    if duration_sec <= 0:
        raise CompositorError(f"duration_sec must be positive, got {duration_sec}")
    frames = max(1, int(round(duration_sec * fps)))
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"zoompan=z='min(zoom+0.0008,1.08)':"
        f"d={frames}:"
        "x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)':"
        f"s=1080x1920:fps={fps}"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "-loop",
            "1",
            "-i",
            str(image),
            "-vf",
            vf,
            "-t",
            f"{duration_sec}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-an",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def concat_segments(segments: list[Path], out: Path) -> Path:
    """Concat via demuxer + stream copy. All inputs must share codec params.

    Writes a sibling ``<out>.concat.txt`` file list, runs ``-f concat -safe 0
    -c copy``, then deletes the list. ``-c copy`` means no re-encode — segments
    produced by :func:`ken_burns` are already aligned (1080x1920, 25fps, h264).
    """
    _require_ffmpeg()
    if not segments:
        raise CompositorError("concat_segments: empty list")

    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.with_suffix(out.suffix + ".concat.txt")
    list_file.write_text(
        "\n".join(f"file '{s.resolve()}'" for s in segments),
        encoding="utf-8",
    )
    try:
        _run(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(out),
            ]
        )
    finally:
        list_file.unlink(missing_ok=True)
    return out


def mux_audio(
    video: Path,
    voice_mp3s: list[Path],
    out: Path,
    bg_music: Path | None = None,
    bg_volume: float = 0.15,
) -> Path:
    """Concat ``voice_mp3s`` to a single AAC track and mux onto ``video``.

    Honors ``-shortest`` to truncate to the shorter of {video, audio}. If
    ``bg_music`` is provided, it's mixed at ``bg_volume`` under the voice via
    ``amix=duration=shortest``.

    The video stream is stream-copied (``-c:v copy``); audio is re-encoded to
    AAC 96 kbps which matches the bitrate fitter's default audio assumption.
    """
    _require_ffmpeg()
    if not voice_mp3s:
        raise CompositorError("mux_audio: empty voice_mp3s list")

    out.parent.mkdir(parents=True, exist_ok=True)
    voice_concat = out.parent / "_voice_concat.aac"
    list_file = out.parent / "_voice_list.txt"

    try:
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in voice_mp3s),
            encoding="utf-8",
        )
        # Concat voice tracks → single AAC bitstream
        _run(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(voice_concat),
            ]
        )

        if bg_music:
            cmd = [
                "-i",
                str(video),
                "-i",
                str(voice_concat),
                "-i",
                str(bg_music),
                "-filter_complex",
                f"[2:a]volume={bg_volume}[bg];"
                f"[1:a][bg]amix=inputs=2:duration=shortest[aout]",
                "-map",
                "0:v",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out),
            ]
        else:
            cmd = [
                "-i",
                str(video),
                "-i",
                str(voice_concat),
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out),
            ]
        _run(cmd)
    finally:
        voice_concat.unlink(missing_ok=True)
        list_file.unlink(missing_ok=True)

    return out


def burn_subtitles(
    video: Path,
    srt: Path,
    out: Path,
    *,
    font_name: str = "Inter",
    font_size: int = 18,
    primary_color_hex: str = "#F4C757",
    outline_color_hex: str = "#1A0F08",
    margin_v: int = 80,
) -> Path:
    """Burn SRT captions onto ``video`` with brand-styled ``force_style``.

    Brand defaults: gold (#F4C757) primary, dark (#1A0F08) outline.

    Pitfall 2 (BGR conversion): primary/outline colours are converted via
    :func:`rgb_to_ass_bgr` before being inlined into ``force_style``.

    Pitfall 3 (paths): the SRT path is single-quoted inside the ``-vf`` arg.
    Callers MUST keep the slug regex ``^[a-z0-9-]+$`` (assemble.py enforces
    upstream — T-11-04-01 mitigation).
    """
    _require_ffmpeg()
    primary_bgr = rgb_to_ass_bgr(primary_color_hex)
    outline_bgr = rgb_to_ass_bgr(outline_color_hex)
    sub_style = (
        f"FontName={font_name},FontSize={font_size},"
        f"PrimaryColour={primary_bgr},OutlineColour={outline_bgr},"
        "BorderStyle=1,Outline=2,Shadow=0,"
        f"Alignment=2,MarginV={margin_v}"
    )
    vf = f"subtitles='{srt}':force_style='{sub_style}'"
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "-i",
            str(video),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "23",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


__all__ = [
    "CompositorError",
    "_require_ffmpeg",
    "ken_burns",
    "concat_segments",
    "mux_audio",
    "burn_subtitles",
    "rgb_to_ass_bgr",
    "DEFAULT_FPS",
    "DEFAULT_RESOLUTION_WH",
]
