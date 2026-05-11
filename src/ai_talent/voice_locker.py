"""Phase 10 voice locker: copy library voice into workspace (PIT-2) + write character.yaml.

Two-step lock:
  1. client.voices.share(public_user_id, voice_id, new_name) — creates workspace
     copy. Without this, original sharer revoke breaks our voice_id (PIT-2).
  2. character_selector.write_voice_ready(...) — atomic YAML mutation with
     additivity invariant (phase_8 + lora unchanged).

PIT-3 override applied: voice_settings.style=0.0 for both presets despite
ROADMAP saying 0.10/0.20. Source: ElevenLabs official docs recommend keeping
style=0 (higher values increase latency and instability). Decision recorded
in 10-RESEARCH.md §Common Pitfalls.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from src.ai_talent.character_selector import (
    DEFAULT_MANIFEST_PATH,
    write_voice_ready,
)


class VoiceLockerError(RuntimeError):
    """Raised when share() fails or workspace lock cannot complete."""


# PIT-3 override (RESEARCH.md): style=0.0 для обоих presets.
# Centry stability=0.4 → warm разговорный для соц-проекта Centry.
# Diktum stability=0.7 → чёткий педагогичный для AI-тренера речи.
DEFAULT_SETTINGS_CENTRY: Final[dict[str, float]] = {
    "stability": 0.4,
    "similarity_boost": 0.75,
    "style": 0.0,
}
DEFAULT_SETTINGS_DIKTUM: Final[dict[str, float]] = {
    "stability": 0.7,
    "similarity_boost": 0.75,
    "style": 0.0,
}
DEFAULT_WORKSPACE_NAME: Final[str] = "forton_lab_v1_ru_female"


def lock_voice(
    client: Any,
    picked_voice_id: str,
    picked_public_user_id: str,
    picked_name: str,
    reference_samples: list[str],
    character_yaml_path: Path = DEFAULT_MANIFEST_PATH,
    *,
    settings_centry: dict[str, float] | None = None,
    settings_diktum: dict[str, float] | None = None,
    locked_by: str | None = None,
    new_workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> dict[str, Any]:
    """Two-step voice lock: workspace copy → character.yaml mutation.

    STEP 1: client.voices.share(...)         — PIT-2 immutable workspace copy
    STEP 2: write_voice_ready(...)           — atomic YAML mutation

    If STEP 1 raises, STEP 2 is NOT attempted (consistency — we don't write the
    YAML referring to a voice_id we don't own).

    Returns the mutated manifest dict from write_voice_ready.
    """
    # STEP 1: copy library voice into our workspace (PIT-2)
    try:
        client.voices.share(
            public_user_id=picked_public_user_id,
            voice_id=picked_voice_id,
            new_name=new_workspace_name,
            bookmarked=True,
        )
    except Exception as e:
        raise VoiceLockerError(
            f"voices.share() failed for voice_id={picked_voice_id!r}: {e!r}"
        ) from e

    # STEP 2: atomic YAML mutate via character_selector
    return write_voice_ready(
        character_yaml_path,
        voice_id=picked_voice_id,
        voice_name=picked_name,
        language="ru",
        reference_samples=reference_samples,
        settings_centry=settings_centry or DEFAULT_SETTINGS_CENTRY,
        settings_diktum=settings_diktum or DEFAULT_SETTINGS_DIKTUM,
        locked_by=locked_by,
    )
