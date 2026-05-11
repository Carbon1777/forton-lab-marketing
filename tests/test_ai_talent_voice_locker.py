"""Tests for Phase 10 Wave 2 — voice_locker (final lock + voices.share + YAML write).

Wave 2 Plan 04 — full implementation.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _bootstrap_phase9_ready(tmp_path: Path) -> Path:
    """Bootstrap manifest past Phase 8 + Phase 9 (lora.status=ready)."""
    from src.ai_talent.character_selector import (
        write_initial_manifest,
        write_lora_ready,
        write_selection,
    )

    yaml_path = tmp_path / "character.yaml"
    write_initial_manifest(yaml_path)

    # Phase 8 selection — pin via mocking compute_batch_sha8
    frame_root = tmp_path / "preview"
    for v in ("variant_1", "variant_2", "variant_3"):
        for c in ("closeup", "medium", "fullbody", "lifestyle"):
            p = frame_root / v / f"{c}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(f"fake-png:{v}:{c}".encode("utf-8"))
    with patch(
        "src.ai_talent.character_selector.compute_batch_sha8",
        return_value="abcd1234",
    ):
        write_selection(
            yaml_path,
            frame_root=frame_root,
            selected="variant_2",
            batch_sha8="abcd1234",
            character_card="test card",
            total_spend_usd=0.3,
        )

    write_lora_ready(
        yaml_path,
        model="x/forton",
        version_sha256="a" * 64,
        training_run_id="t_xyz",
        trigger_word="OHWX_FORTONA",
        training_dataset_size=30,
        training_cost_usd=2.0,
        dataset_path="ai_talent/dataset/v1",
        training_metadata={"steps": 1000, "rank": 16, "trainer_version": "b" * 64},
    )
    return yaml_path


def test_lock_voice_calls_voices_share_once(tmp_path: Path) -> None:
    """PIT-2: voices.share(public_user_id, voice_id, new_name) MUST be called exactly
    once с picked voice_id перед mutation YAML."""
    from src.ai_talent.character_selector import read_manifest
    from src.ai_talent.voice_locker import lock_voice

    yaml_path = _bootstrap_phase9_ready(tmp_path)
    client = MagicMock()

    lock_voice(
        client=client,
        picked_voice_id="EXAVITQu4vr4xnSDxMaL",
        picked_public_user_id="owner_abc",
        picked_name="Татьяна",
        reference_samples=["a.mp3", "b.mp3", "c.mp3"],
        character_yaml_path=yaml_path,
        locked_by="aleksey",
    )

    assert client.voices.share.call_count == 1
    call_kwargs = client.voices.share.call_args.kwargs
    assert call_kwargs["voice_id"] == "EXAVITQu4vr4xnSDxMaL"
    assert call_kwargs["public_user_id"] == "owner_abc"
    assert call_kwargs["new_name"] == "forton_lab_v1_ru_female"
    assert call_kwargs["bookmarked"] is True

    on_disk = read_manifest(yaml_path)
    assert on_disk["voice"]["status"] == "ready"
    assert on_disk["voice"]["voice_id"] == "EXAVITQu4vr4xnSDxMaL"


def test_lock_voice_writes_centry_diktum_split(tmp_path: Path) -> None:
    """character.yaml.voice.voice_settings.centry has stability=0.40,
    .diktum has stability=0.70, both share similarity_boost=0.75, style=0.0 (PIT-3)."""
    from src.ai_talent.character_selector import read_manifest
    from src.ai_talent.voice_locker import lock_voice

    yaml_path = _bootstrap_phase9_ready(tmp_path)
    client = MagicMock()

    lock_voice(
        client=client,
        picked_voice_id="V1",
        picked_public_user_id="o1",
        picked_name="N",
        reference_samples=["a.mp3", "b.mp3", "c.mp3"],
        character_yaml_path=yaml_path,
    )

    voice = read_manifest(yaml_path)["voice"]
    assert voice["voice_settings"]["centry"]["stability"] == 0.4
    assert voice["voice_settings"]["diktum"]["stability"] == 0.7
    assert voice["voice_settings"]["centry"]["style"] == 0.0  # PIT-3
    assert voice["voice_settings"]["diktum"]["style"] == 0.0
    assert voice["voice_settings"]["centry"]["similarity_boost"] == 0.75
    assert voice["voice_settings"]["diktum"]["similarity_boost"] == 0.75


def test_lock_voice_writes_text_cues_supported(tmp_path: Path) -> None:
    """character.yaml.voice.text_cues_supported содержит >=5 паттернов."""
    from src.ai_talent.character_selector import read_manifest
    from src.ai_talent.voice_locker import lock_voice

    yaml_path = _bootstrap_phase9_ready(tmp_path)
    client = MagicMock()

    lock_voice(
        client=client,
        picked_voice_id="V1",
        picked_public_user_id="o1",
        picked_name="N",
        reference_samples=["a.mp3", "b.mp3", "c.mp3"],
        character_yaml_path=yaml_path,
    )

    cues = read_manifest(yaml_path)["voice"]["text_cues_supported"]
    assert len(cues) >= 5


def test_lock_voice_reference_samples_in_assets_dir(tmp_path: Path) -> None:
    """reference_samples список содержит 3-5 mp3 относительных путей под
    assets/voice-reference/<voice_id[:8]>_sample*.mp3."""
    from src.ai_talent.character_selector import read_manifest
    from src.ai_talent.voice_locker import lock_voice

    yaml_path = _bootstrap_phase9_ready(tmp_path)
    client = MagicMock()

    samples = [
        "assets/voice-reference/EXAVITQu_sample1.mp3",
        "assets/voice-reference/EXAVITQu_sample2.mp3",
        "assets/voice-reference/EXAVITQu_sample3.mp3",
        "assets/voice-reference/EXAVITQu_sample4.mp3",
        "assets/voice-reference/EXAVITQu_sample5.mp3",
    ]
    lock_voice(
        client=client,
        picked_voice_id="EXAVITQu4vr4xnSDxMaL",
        picked_public_user_id="o1",
        picked_name="N",
        reference_samples=samples,
        character_yaml_path=yaml_path,
    )

    written = read_manifest(yaml_path)["voice"]["reference_samples"]
    assert written == samples
    assert all(p.startswith("assets/voice-reference/") for p in written)
    assert all("EXAVITQu" in p for p in written)
