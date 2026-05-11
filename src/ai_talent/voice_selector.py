"""Phase 10 voice selection: discovery + reference sample generation.

Phase 10 — first ElevenLabs API surface в repo. Every text_to_speech.convert
вызов gated через BOOT-01 (preflight_check → API → record_provider_spend) and
BOOT-02 (require_paid_tier in _make_client).

Public API split between two waves:
* Plan 02 (this file's first commit): _make_client, search_ru_female_voices
* Plan 03: generate_reference_sample, generate_samples_for_candidate
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from src.elevenlabs_tier import require_paid_tier
from src.spend_tracker_v2 import preflight_check, record_provider_spend

# --- Module constants ---
MODEL_ID: Final[str] = "eleven_multilingual_v2"
OUTPUT_FORMAT: Final[str] = "mp3_44100_128"
# Starter $6 / 30 000 credits; multilingual_v2 = 1 credit / char
COST_PER_CHAR_USD: Final[float] = 0.0002

# Pattern S5 — resolve relative to module, NOT cwd (issue #29 fix)
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_REF_SAMPLES_DIR: Final[Path] = _REPO_ROOT / "assets" / "voice-reference"


class VoiceSelectionError(Exception):
    """Raised on env-misconfig, tier mismatch wrapping, or ElevenLabs-side errors."""


def _make_client():
    """Construct ElevenLabs client; BOOT-02 tier gate runs first.

    Order matters: require_paid_tier() raises before we even look at the API key.
    """
    require_paid_tier()  # BOOT-02 — raises TierMissingError if not paid
    token = os.environ.get("ELEVENLABS_API_KEY")
    if not token:
        raise VoiceSelectionError("ELEVENLABS_API_KEY env var is missing")
    from elevenlabs.client import ElevenLabs
    return ElevenLabs(api_key=token)


_DISCOVERY_FILTERS: Final[tuple[dict[str, Any], ...]] = (
    {"language": "ru", "gender": "female", "category": "professional", "page_size": 50},
    {"language": "ru", "gender": "female", "page_size": 50},
    {"locale": "ru-RU", "gender": "female", "page_size": 50},
    {"search": "russian female", "page_size": 50},
)

_MIN_CANDIDATES: Final[int] = 3


def _voice_to_dict(v: Any) -> dict[str, Any]:
    """Convert SDK LibraryVoiceResponseModel → plain dict for human review.

    Resilient to optional fields (community-submitted labels — see PIT-6).
    """
    return {
        "voice_id": getattr(v, "voice_id", None),
        "name": getattr(v, "name", None),
        "accent": getattr(v, "accent", None),
        "age": getattr(v, "age", None),
        "descriptive": getattr(v, "descriptive", None),
        "labels": dict(getattr(v, "labels", {}) or {}),
        "public_owner_id": (
            getattr(v, "public_owner_id", None)
            or getattr(v, "public_user_id", None)
        ),
        "preview_url": getattr(v, "preview_url", None),
    }


def search_ru_female_voices(
    client: Any | None = None,
    *,
    min_candidates: int = _MIN_CANDIDATES,
) -> list[dict[str, Any]]:
    """Discovery с fallback chain (PIT-4).

    Tries filter combos in order; returns first chain entry where len(voices) >=
    min_candidates. If all entries < min_candidates, returns the last entry's
    result (even if fewer than 3) — caller decides whether to proceed manually.

    NOT gated through preflight_check: voices.get_shared() is free on Starter+
    tier (RESEARCH.md §Standard Stack, ElevenLabs docs API reference).
    """
    if client is None:
        client = _make_client()

    last_result: list[dict[str, Any]] = []
    for filters in _DISCOVERY_FILTERS:
        response = client.voices.get_shared(**filters)
        voices = getattr(response, "voices", None) or []
        candidates = [_voice_to_dict(v) for v in voices]
        last_result = candidates
        if len(candidates) >= min_candidates:
            return candidates
    return last_result


def generate_reference_sample(
    client: Any,
    voice_id: str,
    text: str,
    out_path: Path,
    *,
    spend_file: Path = DEFAULT_SPEND_FILE,
    voice_settings: Any | None = None,
) -> Path:
    """Single ElevenLabs TTS call gated through BOOT-01.

    Flow (order asserted by tests — Phase 9 W-001 carry-forward):
      1. preflight_check("elevenlabs", est_cost_usd=len(text)*COST_PER_CHAR_USD)
      2. client.text_to_speech.convert(voice_id, text, ...)
      3. write joined Iterator[bytes] to disk (PIT-1 — b"".join)
      4. record_provider_spend(..., units=len(text), unit_field="characters")

    If preflight raises, no API call, no file write, no spend record — propagates.

    voice_settings: optional VoiceSettings instance (Plan 04 passes Centry/Diktum
    presets). When None, ElevenLabs falls back to voice default settings.
    """
    char_count = len(text)
    est_cost_usd = char_count * COST_PER_CHAR_USD

    # STEP 1: preflight (mandatory — BOOT-01)
    preflight_check(spend_file, "elevenlabs", est_cost_usd)

    # STEP 2: ElevenLabs API call — returns Iterator[bytes] (PIT-1)
    convert_kwargs: dict[str, Any] = {
        "voice_id": voice_id,
        "text": text,
        "model_id": MODEL_ID,
        "output_format": OUTPUT_FORMAT,
    }
    if voice_settings is not None:
        convert_kwargs["voice_settings"] = voice_settings
    audio_iter = client.text_to_speech.convert(**convert_kwargs)

    # STEP 3: persist (PIT-1 — Iterator joined before write)
    audio_bytes = b"".join(audio_iter)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio_bytes)

    # STEP 4: record spend (mandatory — BOOT-01, Phase 9 W-001 fix)
    record_provider_spend(
        spend_file,
        "elevenlabs",
        usd=est_cost_usd,
        units=char_count,
        unit_field="characters",
    )
    return out_path


def generate_samples_for_candidate(
    voice_id: str,
    voice_name: str,
    texts: list[str],
    out_dir: Path = DEFAULT_REF_SAMPLES_DIR,
    *,
    spend_file: Path = DEFAULT_SPEND_FILE,
    client: Any | None = None,
    voice_settings: Any | None = None,
) -> list[Path]:
    """Generate N reference mp3 samples for one candidate voice.

    Naming: {voice_id[:8]}_sample{N}.mp3 — matches Phase 8 compute_batch_sha8
    8-char prefix convention for human-readable artifact names.
    """
    if client is None:
        client = _make_client()
    paths: list[Path] = []
    for n, text in enumerate(texts, start=1):
        out_path = out_dir / f"{voice_id[:8]}_sample{n}.mp3"
        generate_reference_sample(
            client,
            voice_id,
            text,
            out_path,
            spend_file=spend_file,
            voice_settings=voice_settings,
        )
        paths.append(out_path)
    return paths
