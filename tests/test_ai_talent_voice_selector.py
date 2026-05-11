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


def _make_fake_voices_client(responses: list) -> MagicMock:
    """Mimic ElevenLabs client.voices.get_shared returning preset responses sequentially.

    responses: list of (n_voices, override_dict_for_each_voice).
    """
    client = MagicMock()
    calls: list[dict] = []

    def fake_get_shared(**kwargs):
        calls.append(kwargs)
        idx = min(len(calls) - 1, len(responses) - 1)
        n_voices, override = responses[idx]
        mock_response = MagicMock()
        mock_response.voices = []
        for i in range(n_voices):
            v = MagicMock()
            v.voice_id = f"vid_{idx}_{i}"
            v.name = f"voice_{idx}_{i}"
            v.accent = override.get("accent", "russian")
            v.age = override.get("age", "young")
            v.descriptive = override.get("descriptive", "warm")
            v.labels = {}
            v.public_owner_id = f"owner_{idx}"
            v.preview_url = f"https://preview/{i}"
            mock_response.voices.append(v)
        return mock_response

    client.voices.get_shared.side_effect = fake_get_shared
    client._calls = calls
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


def test_search_returns_ru_female_voices() -> None:
    """search_ru_female_voices() returns >=3 voices через get_shared fallback chain."""
    from src.ai_talent import voice_selector as vs

    client = _make_fake_voices_client([(5, {})])
    result = vs.search_ru_female_voices(client=client, min_candidates=3)

    assert len(result) == 5
    assert all("voice_id" in v for v in result)
    assert all("public_owner_id" in v for v in result)
    assert client.voices.get_shared.call_count == 1


def test_search_fallback_when_filters_too_strict() -> None:
    """Если первая попытка <3 voices, fallback на менее строгие filters (PIT-4)."""
    from src.ai_talent import voice_selector as vs

    client = _make_fake_voices_client([(1, {}), (2, {}), (5, {}), (0, {})])
    result = vs.search_ru_female_voices(client=client, min_candidates=3)

    assert len(result) == 5
    assert client.voices.get_shared.call_count == 3
    calls = client._calls
    assert calls[0].get("category") == "professional"
    assert "category" not in calls[1]
    assert calls[2].get("locale") == "ru-RU"


def test_search_does_not_call_preflight(monkeypatch) -> None:
    """voices.get_shared() — free на Starter+ tier; не gated через spend tracker."""
    from src.ai_talent import voice_selector as vs

    preflight_mock = MagicMock()
    monkeypatch.setattr(vs, "preflight_check", preflight_mock)

    client = _make_fake_voices_client([(3, {})])
    vs.search_ru_female_voices(client=client)

    assert preflight_mock.call_count == 0


def test_make_client_refuses_free_tier(monkeypatch) -> None:
    """_make_client must raise TierMissingError if ELEVENLABS_TIER != paid."""
    from src.elevenlabs_tier import TierMissingError
    from src.ai_talent import voice_selector as vs

    monkeypatch.setenv("ELEVENLABS_TIER", "free")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    with pytest.raises(TierMissingError):
        vs._make_client()


# ============================================================================
# Wave 1 — voice_selector.generate_reference_sample (Plan 03)
# ============================================================================


def test_generate_reference_sample_happy_path(tmp_path: Path, spend_file: Path) -> None:
    """Single text_to_speech.convert -> mp3 на диск + spend recorded (unit_field=characters)."""
    from src.ai_talent import voice_selector as vs

    client = _make_fake_client()
    out_path = tmp_path / "assets" / "voice-reference" / "abcd1234_sample1.mp3"
    text = "Привет, мир. Это тестовое сообщение."  # 36 chars Unicode

    result = vs.generate_reference_sample(
        client=client,
        voice_id="abcd1234567890XYZ",
        text=text,
        out_path=out_path,
        spend_file=spend_file,
    )

    assert result == out_path
    assert out_path.exists()
    assert out_path.read_bytes() == MOCK_MP3_BYTES
    assert client.text_to_speech.convert.call_count == 1

    # Verify model_id and output_format passed correctly
    call_kwargs = client.text_to_speech.convert.call_args.kwargs
    assert call_kwargs["model_id"] == "eleven_multilingual_v2"
    assert call_kwargs["output_format"] == "mp3_44100_128"
    assert call_kwargs["voice_id"] == "abcd1234567890XYZ"
    assert call_kwargs["text"] == text

    # Verify spend recorded with unit_field="characters"
    data = json.loads(spend_file.read_text(encoding="utf-8"))
    months = [k for k in data.keys() if not k.startswith("_")]
    assert len(months) == 1
    entry = data[months[0]]["by_provider"]["elevenlabs"]
    assert entry["calls"] == 1
    assert entry["characters"] == len(text)
    assert entry["usd"] > 0


def test_preflight_runs_before_api_call_and_blocks_on_cap(
    tmp_path: Path, spend_file: Path
) -> None:
    """Phase 9 W-001 carry-forward: if preflight raises → no API call, no file, no record."""
    from src.ai_talent import voice_selector as vs
    from src.spend_tracker_v2 import ProviderMonthlyCapExceededError

    # Pre-fill spend to push elevenlabs over $5 monthly cap
    import datetime as dt
    today = dt.date.today().isoformat()
    month = today[:7]
    sf_data = {
        "_schema_version": 3,
        "_updated": None,
        month: {
            "by_provider": {
                "elevenlabs": {"usd": 4.99, "calls": 1, "characters": 25000}
            },
            "by_day": {
                today: {"usd": 4.99, "by_provider": {"elevenlabs": 4.99}}
            },
        },
    }
    spend_file.write_text(json.dumps(sf_data), encoding="utf-8")

    client = _make_fake_client()
    out_path = tmp_path / "blocked.mp3"
    long_text = "x" * 500  # est_cost = 500 * 0.0002 = $0.10 → pushes over $5 cap

    with pytest.raises(ProviderMonthlyCapExceededError):
        vs.generate_reference_sample(
            client=client,
            voice_id="anyvoice",
            text=long_text,
            out_path=out_path,
            spend_file=spend_file,
        )

    # STEP 2/3/4 must NOT have run
    assert client.text_to_speech.convert.call_count == 0
    assert not out_path.exists()

    # spend tracker untouched after the raise
    data_after = json.loads(spend_file.read_text(encoding="utf-8"))
    assert data_after[month]["by_provider"]["elevenlabs"]["calls"] == 1
    assert data_after[month]["by_provider"]["elevenlabs"]["usd"] == 4.99


def test_iterator_joined_before_write(tmp_path: Path, spend_file: Path) -> None:
    """PIT-1 regression: Iterator[bytes] must be joined before write_bytes."""
    from src.ai_talent import voice_selector as vs

    client = MagicMock()
    chunks = [b"CHUNK_A_", b"CHUNK_B_", b"CHUNK_C"]

    def fake_convert(**kwargs):
        return iter(chunks)

    client.text_to_speech.convert.side_effect = fake_convert
    out_path = tmp_path / "joined.mp3"

    vs.generate_reference_sample(
        client=client,
        voice_id="v",
        text="hello",
        out_path=out_path,
        spend_file=spend_file,
    )

    assert out_path.read_bytes() == b"CHUNK_A_CHUNK_B_CHUNK_C"


def test_generate_samples_for_candidate_writes_3_files(
    tmp_path: Path, spend_file: Path
) -> None:
    """generate_samples_for_candidate(voice_id, 3 texts) -> 3 mp3 files на диске."""
    from src.ai_talent import voice_selector as vs

    client = _make_fake_client()
    out_dir = tmp_path / "voice-reference"
    voice_id = "EXAVITQu4vr4xnSDxMaL"

    paths = vs.generate_samples_for_candidate(
        voice_id=voice_id,
        voice_name="Test",
        texts=["Привет.", "Hello world.", "Test 3."],
        out_dir=out_dir,
        spend_file=spend_file,
        client=client,
    )

    assert len(paths) == 3
    assert all(p.exists() for p in paths)
    assert client.text_to_speech.convert.call_count == 3

    # Naming convention: {voice_id[:8]}_sampleN.mp3
    expected_prefix = voice_id[:8]
    for n, p in enumerate(paths, start=1):
        assert p.name == f"{expected_prefix}_sample{n}.mp3"
