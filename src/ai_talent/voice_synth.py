"""Stage 3 — ElevenLabs TTS with per-character timestamps (PIPE-04).

Two paths:
    * Option A (Q-ELEVEN-TS=YES): ``client.text_to_speech.convert_with_timestamps``
      returns audio + character-level alignment; SRT can be character-accurate.
    * Option C (fallback): ``client.text_to_speech.convert`` returns audio only,
      we write a fallback marker timestamps.json with {fallback: true, text}.
      srt_builder.build_srt sees fallback flag and switches to punctuation-
      distributed SRT (drift ~0.5s per sentence, acceptable for <=30sec videos).

Resolution flag: ``.cache/elevenlabs_timestamps_supported.txt`` written by
Plan 01 probe (Q-ELEVEN-TS). Absence => Option C.

BOOT-01 4-step (asserted by ``test_ai_talent_BOOT_01_invariant``) +
BOOT-02 gate inherited via ``voice_selector._make_client``.

Per-product voice_settings (centry stability=0.4 / diktum stability=0.7)
picked from ``character.yaml.voice.voice_settings``.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Final

import yaml

from src.ai_talent.voice_selector import (
    COST_PER_CHAR_USD,
    MODEL_ID,
    OUTPUT_FORMAT,
    _make_client,
)
from src.spend_tracker_v2 import preflight_check, record_provider_spend

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_CHARACTER_YAML: Final[Path] = _REPO_ROOT / "ai_talent" / "character.yaml"
DEFAULT_TIMESTAMPS_FLAG: Final[Path] = (
    _REPO_ROOT / ".cache" / "elevenlabs_timestamps_supported.txt"
)


class VoiceSynthError(RuntimeError):
    """Raised on env, voice-not-ready, tier mismatch, or ElevenLabs errors."""


def _load_character_yaml(path: Path) -> dict:
    if not path.exists():
        raise VoiceSynthError(f"character.yaml missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _assert_voice_ready(data: dict) -> str:
    voice = (data or {}).get("voice", {}) or {}
    if voice.get("status") != "ready":
        raise VoiceSynthError(
            f"character.yaml.voice.status must be 'ready'; got {voice.get('status')!r}"
        )
    voice_id = voice.get("voice_id")
    if not voice_id:
        raise VoiceSynthError("character.yaml.voice.voice_id missing")
    return voice_id


def pick_settings(char_yaml: dict, product: str) -> Any:
    """Resolve VoiceSettings preset by product.

    Falls back to centry preset if ``product`` not in voice_settings.
    Raises only if neither product nor centry are defined (manifest broken).

    Returns an ``elevenlabs.VoiceSettings`` instance.
    """
    settings_map = (char_yaml.get("voice") or {}).get("voice_settings", {}) or {}
    preset = settings_map.get(product) or settings_map.get("centry")
    if preset is None:
        raise VoiceSynthError(
            f"no voice_settings for product={product!r} (and no centry default)"
        )
    # Local import — voice_selector handles BOOT-02 inside _make_client,
    # so we only need VoiceSettings dataclass here.
    from elevenlabs import VoiceSettings
    return VoiceSettings(
        stability=float(preset["stability"]),
        similarity_boost=float(preset["similarity_boost"]),
        style=float(preset.get("style", 0.0)),
    )


def _timestamps_supported(flag_path: Path = DEFAULT_TIMESTAMPS_FLAG) -> bool:
    """Read Plan 01 probe outcome. True iff Q-ELEVEN-TS resolved YES."""
    if not flag_path.exists():
        return False
    return flag_path.read_text(encoding="utf-8").strip().lower() in {"yes", "true", "1"}


def synthesize_line(
    text: str,
    out_mp3_path: Path,
    *,
    product: str = "centry",
    client: Any | None = None,
    char_yaml_path: Path = DEFAULT_CHARACTER_YAML,
    spend_file: Path = DEFAULT_SPEND_FILE,
    timestamps_flag_path: Path = DEFAULT_TIMESTAMPS_FLAG,
) -> Path:
    """Synthesize one voice line. Writes mp3 + adjacent timestamps.json.

    Flow:
      0. require_paid_tier   (BOOT-02, inside _make_client)
      1. preflight_check     (BOOT-01)
      2. text_to_speech.convert_with_timestamps OR text_to_speech.convert
      3. write mp3 + timestamps.json (with fallback marker if Option C)
      4. record_provider_spend  (BOOT-01)

    timestamps.json shape:
      Option A:  {fallback: False, characters, starts, ends}
      Option C:  {fallback: True, text}
    """
    data = _load_character_yaml(char_yaml_path)
    voice_id = _assert_voice_ready(data)
    settings = pick_settings(data, product)
    if client is None:
        client = _make_client()  # BOOT-02 runs inside

    char_count = len(text)
    est_cost_usd = char_count * COST_PER_CHAR_USD

    # STEP 1: preflight (BOOT-01)
    preflight_check(spend_file, "elevenlabs", est_cost_usd)

    ts_path = out_mp3_path.with_suffix(".timestamps.json")
    out_mp3_path.parent.mkdir(parents=True, exist_ok=True)

    # STEP 2: API call — prefer convert_with_timestamps if probe-supported
    use_timestamps = (
        _timestamps_supported(timestamps_flag_path)
        and hasattr(client.text_to_speech, "convert_with_timestamps")
    )
    if use_timestamps:
        result = client.text_to_speech.convert_with_timestamps(
            voice_id=voice_id,
            text=text,
            model_id=MODEL_ID,
            output_format=OUTPUT_FORMAT,
            voice_settings=settings,
        )
        audio_b64 = (
            getattr(result, "audio_base64", None)
            if not isinstance(result, dict)
            else result.get("audio_base64")
        )
        if audio_b64 is None:
            raise VoiceSynthError(
                "convert_with_timestamps returned no audio_base64 field"
            )
        audio_bytes = base64.b64decode(audio_b64)

        alignment = (
            getattr(result, "alignment", None)
            if not isinstance(result, dict)
            else result.get("alignment")
        )
        if alignment is None:
            ts_payload: dict[str, Any] = {"fallback": True, "text": text}
        else:
            chars = (
                getattr(alignment, "characters", None)
                if not isinstance(alignment, dict)
                else alignment.get("characters", [])
            )
            starts = (
                getattr(alignment, "character_start_times_seconds", None)
                if not isinstance(alignment, dict)
                else alignment.get("character_start_times_seconds", [])
            )
            ends = (
                getattr(alignment, "character_end_times_seconds", None)
                if not isinstance(alignment, dict)
                else alignment.get("character_end_times_seconds", [])
            )
            ts_payload = {
                "fallback": False,
                "characters": list(chars or []),
                "starts": list(starts or []),
                "ends": list(ends or []),
            }
    else:
        audio_iter = client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id=MODEL_ID,
            output_format=OUTPUT_FORMAT,
            voice_settings=settings,
        )
        audio_bytes = b"".join(audio_iter)
        ts_payload = {"fallback": True, "text": text}

    # STEP 3: persist
    out_mp3_path.write_bytes(audio_bytes)
    ts_path.write_text(
        json.dumps(ts_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    # STEP 4: record spend (BOOT-01)
    record_provider_spend(
        spend_file, "elevenlabs",
        usd=est_cost_usd,
        units=char_count,
        unit_field="characters",
    )
    return out_mp3_path


__all__ = [
    "VoiceSynthError",
    "synthesize_line",
    "pick_settings",
    "DEFAULT_TIMESTAMPS_FLAG",
]
