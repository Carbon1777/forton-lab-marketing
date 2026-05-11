"""Stage 5 — SRT generation from ElevenLabs timestamps OR fallback approximation.

Two paths, chosen per-line based on ``timestamps.json[i].fallback`` flag set by
``voice_synth.synthesize_line``:

    * Option A — character-level alignment from ``convert_with_timestamps``
      (Q-ELEVEN-TS = YES). SRT can split on sentence boundary or 50-char chunks.
    * Option C — punctuation-distributed approximation. Drift ~0.5s/sentence,
      acceptable for ≤30sec videos (RESEARCH §Pattern 5).

Output format: standard SRT with ``HH:MM:SS,mmm`` timestamps and monotonic
1-based numbering.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final


class SrtBuilderError(RuntimeError):
    """Raised on missing inputs, length mismatch, or malformed timestamps."""


_MAX_CHUNK_CHARS: Final[int] = 50
_SENTENCE_TERMINATORS: Final[str] = ".!?…"


def _srt_ts(sec: float) -> str:
    """Convert seconds to SRT ``HH:MM:SS,mmm`` format.

    Negative seconds clamp to 0. Sub-second milliseconds rounded to 3 digits.
    """
    if sec < 0:
        sec = 0.0
    total_ms = int(round(sec * 1000))
    h = total_ms // 3_600_000
    rem = total_ms - h * 3_600_000
    m = rem // 60_000
    rem -= m * 60_000
    s = rem // 1000
    ms = rem - s * 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _from_char_alignment(
    payload: dict, line_offset: float
) -> list[tuple[float, float, str]]:
    """Group character-level timestamps into ~50-char SRT chunks.

    Returns list of (start_abs, end_abs, text) tuples with offset applied.
    Splits on either sentence terminator or 50-character soft limit.
    """
    chars = payload.get("characters") or []
    starts = payload.get("starts") or []
    ends = payload.get("ends") or []
    if not (len(chars) == len(starts) == len(ends)):
        raise SrtBuilderError(
            f"characters/starts/ends length mismatch: "
            f"{len(chars)}/{len(starts)}/{len(ends)}"
        )
    if not chars:
        return []

    chunks: list[tuple[float, float, str]] = []
    buf: list[str] = []
    buf_start: float | None = None
    last_end: float = 0.0

    for ch, st, en in zip(chars, starts, ends):
        if buf_start is None:
            buf_start = float(st)
        buf.append(ch)
        last_end = float(en)
        joined = "".join(buf)
        if len(joined) >= _MAX_CHUNK_CHARS or ch in _SENTENCE_TERMINATORS:
            txt = joined.strip()
            if txt:
                chunks.append((buf_start + line_offset, last_end + line_offset, txt))
            buf = []
            buf_start = None

    if buf:
        txt = "".join(buf).strip()
        if txt and buf_start is not None:
            chunks.append((buf_start + line_offset, last_end + line_offset, txt))

    return chunks


def _from_text_approx(
    text: str, audio_duration_sec: float, line_offset: float
) -> list[tuple[float, float, str]]:
    """Punctuation-distributed SRT chunks for fallback path (Option C).

    Splits ``text`` into sentences on ``.!?…``, distributes ``audio_duration_sec``
    proportionally to character count of each sentence. Drift acceptable for
    short videos.
    """
    if not text.strip() or audio_duration_sec <= 0:
        return []
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()
    ]
    if not sentences:
        return []
    total_chars = sum(len(s) for s in sentences)
    if total_chars == 0:
        return []

    chunks: list[tuple[float, float, str]] = []
    cursor = line_offset
    for s in sentences:
        share = (len(s) / total_chars) * audio_duration_sec
        end = cursor + share
        chunks.append((cursor, end, s))
        cursor = end
    return chunks


def build_srt(
    timestamps_paths: list[Path],
    voice_line_texts: list[str],
    out_path: Path,
    *,
    line_offsets_sec: list[float] | None = None,
    audio_durations_sec: list[float] | None = None,
) -> Path:
    """Stitch per-line timestamps OR text-approximations into a single SRT.

    Args:
        timestamps_paths: per-line timestamps.json paths written by
            voice_synth.synthesize_line. Each JSON has ``fallback`` flag.
        voice_line_texts: matching list of voice_line texts (same order).
        out_path: target .srt file.
        line_offsets_sec: where each line starts in the final timeline.
            Defaults to all zeros (single-line case).
        audio_durations_sec: fallback duration used by Option C when a line's
            timestamps.json has ``fallback=True``. Defaults to 3.0s per line.

    Returns ``out_path``.
    """
    if len(timestamps_paths) != len(voice_line_texts):
        raise SrtBuilderError(
            f"timestamps_paths ({len(timestamps_paths)}) != "
            f"voice_line_texts ({len(voice_line_texts)})"
        )
    n = len(timestamps_paths)
    if line_offsets_sec is None:
        line_offsets_sec = [0.0] * n
    if audio_durations_sec is None:
        audio_durations_sec = [3.0] * n
    if len(line_offsets_sec) != n or len(audio_durations_sec) != n:
        raise SrtBuilderError(
            "line_offsets_sec and audio_durations_sec must match "
            f"timestamps_paths length ({n})"
        )

    all_chunks: list[tuple[float, float, str]] = []
    for ts_path, text, off, dur in zip(
        timestamps_paths, voice_line_texts, line_offsets_sec, audio_durations_sec
    ):
        if not Path(ts_path).exists():
            raise SrtBuilderError(f"timestamps.json missing: {ts_path}")
        payload = json.loads(Path(ts_path).read_text(encoding="utf-8"))
        if payload.get("fallback"):
            all_chunks.extend(_from_text_approx(text, float(dur), float(off)))
        else:
            all_chunks.extend(_from_char_alignment(payload, float(off)))

    lines: list[str] = []
    for i, (start, end, txt) in enumerate(all_chunks, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(txt)
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path


__all__ = ["SrtBuilderError", "build_srt", "_srt_ts"]
