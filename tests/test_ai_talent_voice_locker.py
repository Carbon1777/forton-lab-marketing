"""Tests for Phase 10 Wave 2 — voice_locker (final lock + voices.share + YAML write).

Wave 0 = skipped scaffolds. Wave 2 Plan 04 removes skip markers.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.mark.skip(reason="Wave 2 Plan 04 will implement voice_locker.lock_voice")
def test_lock_voice_calls_voices_share_once() -> None:
    """PIT-2: voices.share(public_user_id, voice_id, new_name) MUST be called exactly
    once с picked voice_id перед mutation YAML."""
    pass


@pytest.mark.skip(reason="Wave 2 Plan 04 — settings split per VOICE-02")
def test_lock_voice_writes_centry_diktum_split() -> None:
    """character.yaml.voice.voice_settings.centry has stability=0.40,
    .diktum has stability=0.70, both share similarity_boost=0.75, style=0.0 (PIT-3)."""
    pass


@pytest.mark.skip(reason="Wave 2 Plan 04 — text cues VOICE-03")
def test_lock_voice_writes_text_cues_supported() -> None:
    """character.yaml.voice.text_cues_supported содержит >=5 паттернов."""
    pass


@pytest.mark.skip(reason="Wave 2 Plan 04 — file naming convention")
def test_lock_voice_reference_samples_in_assets_dir() -> None:
    """reference_samples список содержит 3-5 mp3 относительных путей под
    assets/voice-reference/<voice_id[:8]>_sample*.mp3."""
    pass
