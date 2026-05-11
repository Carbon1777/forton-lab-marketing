"""Phase 11 Plan 05 — voice_synth.py (Stage 3) tests.

Coverage:
    - pick_settings per product (centry / diktum / unknown fallback)
    - voice-ready gate (status != ready -> error)
    - BOOT-01 ordering with mocked ElevenLabs client + spend tracker
    - characters unit_field
    - timestamps.json adjacency (Option A and Option C marker)
    - timestamp probe flag toggles convert path
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml


_VOICE_ID = "GN4wbsbejSnGSa1AzjH5"


def _make_char_yaml(tmp_path: Path, voice_status: str = "ready") -> Path:
    p = tmp_path / "character.yaml"
    p.write_text(yaml.safe_dump({
        "voice": {
            "status": voice_status,
            "voice_id": _VOICE_ID,
            "model_id": "eleven_multilingual_v2",
            "output_format": "mp3_44100_128",
            "voice_settings": {
                "centry": {"stability": 0.4, "similarity_boost": 0.75, "style": 0.0},
                "diktum": {"stability": 0.7, "similarity_boost": 0.75, "style": 0.0},
            },
        },
    }), encoding="utf-8")
    return p


@pytest.fixture
def char_yaml_ready(tmp_path):
    return _make_char_yaml(tmp_path, voice_status="ready")


@pytest.fixture
def char_yaml_pending(tmp_path):
    return _make_char_yaml(tmp_path, voice_status="pending")


@pytest.fixture
def mock_eleven_client_convert():
    """Client whose .text_to_speech.convert returns iter of bytes."""
    client = MagicMock()
    # Make sure convert_with_timestamps is NOT present so fallback path is hit
    # unless test patches it in.
    del client.text_to_speech.convert_with_timestamps
    client.text_to_speech.convert.return_value = iter([b"ID3" + b"\x00" * 64])
    return client


@pytest.fixture
def flag_off(tmp_path):
    """Timestamps probe flag absent -> Option C path."""
    return tmp_path / "_does_not_exist_flag.txt"


@pytest.fixture
def flag_on(tmp_path):
    p = tmp_path / "flag.txt"
    p.write_text("yes\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# pick_settings
# --------------------------------------------------------------------------


def test_pick_settings_centry(char_yaml_ready):
    from src.ai_talent.voice_synth import pick_settings
    data = yaml.safe_load(char_yaml_ready.read_text())
    settings = pick_settings(data, "centry")
    assert settings.stability == 0.4
    assert settings.similarity_boost == 0.75


def test_pick_settings_diktum(char_yaml_ready):
    from src.ai_talent.voice_synth import pick_settings
    data = yaml.safe_load(char_yaml_ready.read_text())
    settings = pick_settings(data, "diktum")
    assert settings.stability == 0.7


def test_pick_settings_unknown_product_falls_back_to_centry(char_yaml_ready):
    from src.ai_talent.voice_synth import pick_settings
    data = yaml.safe_load(char_yaml_ready.read_text())
    settings = pick_settings(data, "nonsense-product")
    # Falls back to centry preset (stability 0.4)
    assert settings.stability == 0.4


def test_pick_settings_raises_when_no_presets(tmp_path):
    from src.ai_talent.voice_synth import VoiceSynthError, pick_settings
    p = tmp_path / "x.yaml"
    p.write_text(yaml.safe_dump({"voice": {"voice_settings": {}}}), encoding="utf-8")
    data = yaml.safe_load(p.read_text())
    with pytest.raises(VoiceSynthError, match="no voice_settings"):
        pick_settings(data, "centry")


# --------------------------------------------------------------------------
# voice-ready gate
# --------------------------------------------------------------------------


def test_synthesize_line_raises_when_voice_not_ready(
    char_yaml_pending, mock_eleven_client_convert, tmp_path, tmp_spend_file, flag_off
):
    from src.ai_talent.voice_synth import VoiceSynthError, synthesize_line
    out = tmp_path / "line.mp3"
    with pytest.raises(VoiceSynthError, match="'ready'"):
        synthesize_line(
            "Привет.",
            out,
            product="centry",
            client=mock_eleven_client_convert,
            char_yaml_path=char_yaml_pending,
            spend_file=tmp_spend_file,
            timestamps_flag_path=flag_off,
        )
    mock_eleven_client_convert.text_to_speech.convert.assert_not_called()


# --------------------------------------------------------------------------
# BOOT-01 ordering — Option C fallback path
# --------------------------------------------------------------------------


def test_synthesize_line_BOOT_01_ordering_option_c(
    char_yaml_ready, mock_eleven_client_convert, tmp_path, tmp_spend_file,
    flag_off, mocker,
):
    from src.ai_talent import voice_synth as vs

    call_order: list[str] = []
    mocker.patch.object(
        vs, "preflight_check",
        side_effect=lambda *a, **kw: call_order.append("preflight"),
    )

    def fake_convert(*a, **kw):
        call_order.append("convert")
        return iter([b"ID3" + b"\x00" * 32])

    mock_eleven_client_convert.text_to_speech.convert.side_effect = fake_convert
    mocker.patch.object(
        vs, "record_provider_spend",
        side_effect=lambda *a, **kw: call_order.append("record"),
    )

    out = tmp_path / "line.mp3"
    vs.synthesize_line(
        "Привет.",
        out,
        product="centry",
        client=mock_eleven_client_convert,
        char_yaml_path=char_yaml_ready,
        spend_file=tmp_spend_file,
        timestamps_flag_path=flag_off,
    )
    assert call_order == ["preflight", "convert", "record"]
    assert out.exists()


def test_synthesize_line_writes_fallback_timestamps_json_option_c(
    char_yaml_ready, mock_eleven_client_convert, tmp_path, tmp_spend_file, flag_off
):
    from src.ai_talent.voice_synth import synthesize_line
    out = tmp_path / "line.mp3"
    synthesize_line(
        "Привет мир.",
        out,
        product="centry",
        client=mock_eleven_client_convert,
        char_yaml_path=char_yaml_ready,
        spend_file=tmp_spend_file,
        timestamps_flag_path=flag_off,
    )
    ts_path = out.with_suffix(".timestamps.json")
    assert ts_path.exists()
    payload = json.loads(ts_path.read_text(encoding="utf-8"))
    assert payload["fallback"] is True
    assert payload["text"] == "Привет мир."


def test_synthesize_line_records_characters_units(
    char_yaml_ready, mock_eleven_client_convert, tmp_path, tmp_spend_file, flag_off
):
    from src.ai_talent.voice_synth import synthesize_line
    text = "Привет."
    out = tmp_path / "line.mp3"
    synthesize_line(
        text, out,
        product="centry",
        client=mock_eleven_client_convert,
        char_yaml_path=char_yaml_ready,
        spend_file=tmp_spend_file,
        timestamps_flag_path=flag_off,
    )
    spend = json.loads(tmp_spend_file.read_text())
    month_keys = [k for k in spend if k[:4].isdigit() and "-" in k]
    month = spend[month_keys[0]]
    eleven = month["by_provider"]["elevenlabs"]
    assert eleven["characters"] == len(text)


def test_synthesize_line_passes_per_product_voice_settings(
    char_yaml_ready, mock_eleven_client_convert, tmp_path, tmp_spend_file, flag_off
):
    from src.ai_talent.voice_synth import synthesize_line
    out = tmp_path / "line.mp3"
    synthesize_line(
        "Diktum line.",
        out,
        product="diktum",
        client=mock_eleven_client_convert,
        char_yaml_path=char_yaml_ready,
        spend_file=tmp_spend_file,
        timestamps_flag_path=flag_off,
    )
    kwargs = mock_eleven_client_convert.text_to_speech.convert.call_args.kwargs
    assert kwargs["voice_id"] == _VOICE_ID
    # Diktum preset stability=0.7
    assert kwargs["voice_settings"].stability == 0.7


# --------------------------------------------------------------------------
# Option A path — convert_with_timestamps
# --------------------------------------------------------------------------


def test_synthesize_line_uses_timestamps_path_when_flag_on(
    char_yaml_ready, tmp_path, tmp_spend_file, flag_on
):
    """When probe flag is YES and client exposes convert_with_timestamps,
    that path runs and the alignment is written to timestamps.json."""
    from src.ai_talent.voice_synth import synthesize_line

    audio_b64 = base64.b64encode(b"ID3" + b"\x00" * 32).decode("ascii")
    alignment = {
        "characters": ["П", "р", "и", "в", "е", "т", "."],
        "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        "character_end_times_seconds":   [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
    }
    result = MagicMock()
    result.audio_base64 = audio_b64
    result.alignment = MagicMock(
        characters=alignment["characters"],
        character_start_times_seconds=alignment["character_start_times_seconds"],
        character_end_times_seconds=alignment["character_end_times_seconds"],
    )
    client = MagicMock()
    client.text_to_speech.convert_with_timestamps.return_value = result

    out = tmp_path / "line.mp3"
    synthesize_line(
        "Привет.",
        out,
        product="centry",
        client=client,
        char_yaml_path=char_yaml_ready,
        spend_file=tmp_spend_file,
        timestamps_flag_path=flag_on,
    )
    client.text_to_speech.convert_with_timestamps.assert_called_once()
    ts_payload = json.loads(out.with_suffix(".timestamps.json").read_text())
    assert ts_payload["fallback"] is False
    assert ts_payload["characters"] == alignment["characters"]
    assert ts_payload["starts"] == alignment["character_start_times_seconds"]


def test_synthesize_line_indirectly_invokes_BOOT_02_via_voice_selector():
    """voice_synth imports _make_client from voice_selector — BOOT-02
    require_paid_tier runs inside that. The grep-invariant test verifies the
    import; this test asserts the symbol path exists at runtime."""
    from src.ai_talent.voice_synth import _make_client as imported
    from src.ai_talent.voice_selector import _make_client as origin
    assert imported is origin


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def test_synthesize_line_raises_when_yaml_missing(
    mock_eleven_client_convert, tmp_path, tmp_spend_file, flag_off
):
    from src.ai_talent.voice_synth import VoiceSynthError, synthesize_line
    with pytest.raises(VoiceSynthError, match="character.yaml missing"):
        synthesize_line(
            "x", tmp_path / "x.mp3",
            product="centry",
            client=mock_eleven_client_convert,
            char_yaml_path=tmp_path / "absent.yaml",
            spend_file=tmp_spend_file,
            timestamps_flag_path=flag_off,
        )
