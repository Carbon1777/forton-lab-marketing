"""Tests for Phase 10 Wave 1 — voice_selector (ElevenLabs discovery + sampler).

Wave 0 = skipped scaffolds locking the contract. Wave 1/2 removes skip markers.

Covers BOOT-01 invariant: every text_to_speech.convert() call is preceded by
preflight_check and followed by record_provider_spend(unit_field="characters").
Covers BOOT-02 invariant: client init refuses on non-paid tier.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MOCK_MP3_BYTES = (FIXTURES_DIR / "mock_elevenlabs_output.mp3").read_bytes()


def _make_fake_client(captured: list | None = None) -> MagicMock:
    """Mimic ElevenLabs client; convert() returns Iterator[bytes] chunks (PIT-1)."""
    client = MagicMock()

    def fake_convert(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return iter([MOCK_MP3_BYTES])

    client.text_to_speech.convert.side_effect = fake_convert
    return client


@pytest.fixture
def spend_file(tmp_path: Path) -> Path:
    metrics = tmp_path / ".metrics"
    metrics.mkdir()
    sf = metrics / "api_spend.json"
    sf.write_text(
        json.dumps({"_schema_version": 3, "_updated": None}),
        encoding="utf-8",
    )
    return sf


# ============================================================================
# Wave 1 — voice_selector.search_ru_female_voices (Plan 02)
# ============================================================================


@pytest.mark.skip(reason="Wave 1 Plan 02 will implement voice_selector.search_ru_female_voices")
def test_search_returns_ru_female_voices() -> None:
    """search_ru_female_voices() returns >=3 voices через get_shared fallback chain."""
    pass


@pytest.mark.skip(reason="Wave 1 Plan 02 will implement voice_selector fallback chain")
def test_search_fallback_when_filters_too_strict() -> None:
    """Если первая попытка <3 voices, fallback на менее строгие filters (PIT-4)."""
    pass


@pytest.mark.skip(reason="Wave 1 Plan 02 voice_selector should NOT call preflight (search free)")
def test_search_does_not_call_preflight() -> None:
    """voices.get_shared() — free на Starter+ tier; не gated через spend tracker."""
    pass


# ============================================================================
# Wave 1 — voice_selector.generate_reference_sample (Plan 03)
# ============================================================================


@pytest.mark.skip(reason="Wave 1 Plan 03 will implement generate_reference_sample")
def test_generate_reference_sample_happy_path() -> None:
    """Single text_to_speech.convert -> mp3 на диск + spend recorded (unit_field=characters)."""
    pass


@pytest.mark.skip(reason="Wave 1 Plan 03 — Phase 9 W-001 carry-forward")
def test_preflight_runs_before_api_call_and_blocks_on_cap() -> None:
    """Pre-fill spend over $5 elevenlabs cap -> ProviderMonthlyCapExceededError;
    convert() NEVER called, mp3 NOT written, spend NOT recorded after."""
    pass


@pytest.mark.skip(reason="Wave 1 Plan 03 — PIT-1 regression test")
def test_iterator_joined_before_write() -> None:
    """Iterator[bytes] from convert() must be joined via b''.join() — file must be
    non-zero bytes, not the repr of an iterator."""
    pass


@pytest.mark.skip(reason="Wave 1 Plan 03 — BOOT-02 tier gate")
def test_make_client_refuses_free_tier(monkeypatch) -> None:
    """_make_client must raise TierMissingError if ELEVENLABS_TIER != paid."""
    pass


@pytest.mark.skip(reason="Wave 1 Plan 03 — batch generation per candidate")
def test_generate_samples_for_candidate_writes_3_files() -> None:
    """generate_samples_for_candidate(voice_id, 3 texts) -> 3 mp3 files на диске."""
    pass
