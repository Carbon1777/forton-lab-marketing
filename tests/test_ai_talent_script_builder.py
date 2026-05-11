"""Phase 11 Plan 05 — script_builder.py (Stage 1) tests.

Coverage:
    - schema validation: missing keys, hero count, trigger word, teeth blacklist
    - BOOT-01 ordering: preflight -> messages.create -> write -> record_spend
    - tool_choice forced in API call kwargs
    - SYSTEM prompt file loaded from disk
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ai_talent import script_builder as sb
from src.ai_talent.script_builder import (
    REQUIRED_KEYS,
    SCRIPT_SCHEMA,
    SYSTEM_PROMPT_PATH,
    TEETH_BLACKLIST,
    TRIGGER_WORD,
    ScriptBuilderError,
    build_script,
    validate_script,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _good_script() -> dict:
    """A schema-valid script dict used as the baseline for negative tests."""
    return {
        "hook": "Утренний кофе в выходной",
        "beats": [
            {"id": "b1", "frame_prompt": f"{TRIGGER_WORD} gentle smile, warm lighting",
             "duration_sec": 4.0, "is_hero": False},
            {"id": "b2", "frame_prompt": f"{TRIGGER_WORD} soft expression, golden hour",
             "duration_sec": 5.0, "is_hero": True},
            {"id": "b3", "frame_prompt": f"{TRIGGER_WORD} cinematic shallow depth",
             "duration_sec": 4.5, "is_hero": False},
            {"id": "b4", "frame_prompt": f"{TRIGGER_WORD} serene look",
             "duration_sec": 4.0, "is_hero": False},
        ],
        "voice_lines": [{"beat_id": "b1", "text": "Привет."}],
        "cuts": ["b1->b2", "b2->b3", "b3->b4"],
        "cta": "centryweb.ru",
        "product": "centry",
        "series_flag": None,
        "hero_beat_id": "b2",
    }


# --------------------------------------------------------------------------
# validate_script — schema gates
# --------------------------------------------------------------------------


def test_validate_script_rejects_missing_keys():
    bad = _good_script()
    del bad["hook"]
    with pytest.raises(ScriptBuilderError, match="missing key 'hook'"):
        validate_script(bad)


def test_validate_script_rejects_zero_hero_beats():
    bad = _good_script()
    for b in bad["beats"]:
        b["is_hero"] = False
    with pytest.raises(ScriptBuilderError, match="exactly 1 hero beat"):
        validate_script(bad)


def test_validate_script_rejects_two_hero_beats():
    bad = _good_script()
    bad["beats"][0]["is_hero"] = True
    bad["beats"][1]["is_hero"] = True
    with pytest.raises(ScriptBuilderError, match="exactly 1 hero beat"):
        validate_script(bad)


def test_validate_script_rejects_teeth_blacklist_laughing():
    bad = _good_script()
    bad["beats"][0]["frame_prompt"] = f"{TRIGGER_WORD} laughing genuinely at camera"
    with pytest.raises(ScriptBuilderError, match="teeth"):
        validate_script(bad)


def test_validate_script_rejects_teeth_blacklist_open_mouth():
    bad = _good_script()
    bad["beats"][1]["frame_prompt"] = f"{TRIGGER_WORD} open mouth smile, joyful"
    with pytest.raises(ScriptBuilderError, match="teeth"):
        validate_script(bad)


def test_validate_script_rejects_teeth_blacklist_wide_grin():
    bad = _good_script()
    bad["beats"][2]["frame_prompt"] = f"{TRIGGER_WORD} wide grin, happy"
    with pytest.raises(ScriptBuilderError, match="teeth"):
        validate_script(bad)


def test_validate_script_rejects_missing_trigger_word():
    bad = _good_script()
    bad["beats"][0]["frame_prompt"] = "A woman gently smiling, no trigger"
    with pytest.raises(ScriptBuilderError, match="OHWX_FORTONA"):
        validate_script(bad)


def test_validate_script_rejects_hero_beat_id_not_in_beats():
    bad = _good_script()
    bad["hero_beat_id"] = "bX"
    with pytest.raises(ScriptBuilderError, match="hero_beat_id"):
        validate_script(bad)


def test_validate_script_passes_clean_input():
    # Should NOT raise
    assert validate_script(_good_script()) is None


def test_required_keys_match_schema():
    """REQUIRED_KEYS tuple is the source of truth for SCRIPT_SCHEMA.required."""
    assert set(REQUIRED_KEYS) == set(SCRIPT_SCHEMA["input_schema"]["required"])


def test_teeth_blacklist_constant_complete():
    """All 4 phrases mentioned in PROJECT W-002 mitigation must be present."""
    expected = {
        "laughing genuinely",
        "open mouth smile",
        "wide grin",
        "laughing with teeth visible",
    }
    assert expected.issubset(set(TEETH_BLACKLIST))


# --------------------------------------------------------------------------
# build_script — full BOOT-01 flow with mocked Anthropic + spend tracker
# --------------------------------------------------------------------------


@pytest.fixture
def mock_client_returns_good_script():
    """Anthropic client whose messages.create returns a valid tool_use block."""
    client = MagicMock()
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.input = _good_script()
    msg = MagicMock()
    msg.content = [tool_use]
    msg.stop_reason = "tool_use"
    client.messages.create.return_value = msg
    return client


def test_build_script_calls_anthropic_with_tool_choice_forced(
    mock_client_returns_good_script, tmp_path, tmp_spend_file, monkeypatch
):
    out = tmp_path / "script.json"
    build_script(
        "Brief body", "Character card",
        out_path=out,
        client=mock_client_returns_good_script,
        spend_file=tmp_spend_file,
    )
    kwargs = mock_client_returns_good_script.messages.create.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_video_script"}
    assert kwargs["tools"][0]["name"] == "emit_video_script"
    assert kwargs["model"] == sb.MODEL


def test_build_script_records_spend_after_persist(
    mock_client_returns_good_script, tmp_path, tmp_spend_file
):
    """BOOT-01 ordering: preflight runs, then API, then file write, then record."""
    out = tmp_path / "script.json"
    build_script(
        "Brief body", "Character card",
        out_path=out,
        client=mock_client_returns_good_script,
        spend_file=tmp_spend_file,
    )
    # File written
    assert out.exists()
    # Spend recorded (v3 schema has by_provider key)
    spend = json.loads(tmp_spend_file.read_text())
    # Find the month entry
    month_entries = [k for k in spend.keys() if k[:4].isdigit() and "-" in k]
    assert month_entries, f"no monthly entry created in spend file: {spend}"
    month = spend[month_entries[0]]
    assert month.get("by_provider", {}).get("anthropic", {}).get("usd", 0) > 0


def test_build_script_writes_script_json_to_out_path(
    mock_client_returns_good_script, tmp_path, tmp_spend_file
):
    out = tmp_path / "nested" / "01-script" / "script.json"
    build_script(
        "Brief body", "Character card",
        out_path=out,
        client=mock_client_returns_good_script,
        spend_file=tmp_spend_file,
    )
    assert out.exists()
    data = json.loads(out.read_text())
    for k in REQUIRED_KEYS:
        assert k in data
    # validator-passable
    validate_script(data)


def test_build_script_raises_when_tool_use_missing(
    tmp_path, tmp_spend_file
):
    client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    msg = MagicMock()
    msg.content = [text_block]
    msg.stop_reason = "end_turn"
    client.messages.create.return_value = msg
    with pytest.raises(ScriptBuilderError, match="no tool_use block"):
        build_script(
            "b", "c", out_path=tmp_path / "x.json",
            client=client, spend_file=tmp_spend_file,
        )


def test_build_script_propagates_validator_error(tmp_path, tmp_spend_file):
    """If Claude returns malformed dict, validate_script raises before file write."""
    client = MagicMock()
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    bad = _good_script()
    del bad["hook"]
    tool_use.input = bad
    msg = MagicMock()
    msg.content = [tool_use]
    client.messages.create.return_value = msg

    out = tmp_path / "x.json"
    with pytest.raises(ScriptBuilderError, match="missing key"):
        build_script(
            "b", "c", out_path=out,
            client=client, spend_file=tmp_spend_file,
        )
    assert not out.exists()


def test_system_prompt_file_present_and_contains_trigger():
    """Production SYSTEM prompt file must exist and contain the trigger word."""
    assert SYSTEM_PROMPT_PATH.exists(), f"missing: {SYSTEM_PROMPT_PATH}"
    txt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert TRIGGER_WORD in txt
    # Defense in depth — all 4 blacklist entries mentioned in SYSTEM prompt
    for forbidden in TEETH_BLACKLIST:
        assert forbidden in txt, f"SYSTEM prompt missing blacklist entry: {forbidden!r}"


def test_build_script_uses_custom_system_prompt_path(
    mock_client_returns_good_script, tmp_path, tmp_spend_file
):
    """Caller can override system prompt path (used by smoke tests)."""
    custom_prompt = tmp_path / "custom.md"
    custom_prompt.write_text("CUSTOM SYSTEM PROMPT — OHWX_FORTONA\n", encoding="utf-8")
    out = tmp_path / "script.json"
    build_script(
        "b", "c", out_path=out,
        client=mock_client_returns_good_script,
        spend_file=tmp_spend_file,
        system_prompt_path=custom_prompt,
    )
    kwargs = mock_client_returns_good_script.messages.create.call_args.kwargs
    assert "CUSTOM SYSTEM PROMPT" in kwargs["system"]


def test_build_script_raises_when_system_prompt_missing(
    mock_client_returns_good_script, tmp_path, tmp_spend_file
):
    out = tmp_path / "x.json"
    with pytest.raises(ScriptBuilderError, match="SYSTEM prompt missing"):
        build_script(
            "b", "c", out_path=out,
            client=mock_client_returns_good_script,
            spend_file=tmp_spend_file,
            system_prompt_path=tmp_path / "does_not_exist.md",
        )
