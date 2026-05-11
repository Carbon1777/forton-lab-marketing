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
