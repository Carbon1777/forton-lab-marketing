"""Phase 8 Plan 04 — character_selector tests.

Covers schema round-trip, anti-replay (sha8 mismatch), variant validation,
and additive-schema tolerance (Phase 9/10 forward-compat).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.ai_talent.character_selector import (
    LoraTriggerMismatchError,
    SelectionMismatchError,
    _normalize_version_sha,
    read_manifest,
    write_initial_manifest,
    write_lora_ready,
    write_selection,
)
from src.ai_talent.preview_sender import compute_batch_sha8


VARIANTS = ("variant_1", "variant_2", "variant_3")
FRAMES = ("closeup", "medium", "fullbody", "lifestyle")


def _make_frames(root: Path) -> list[Path]:
    """Create 12 deterministic tiny PNG files. Returns sorted paths."""
    paths: list[Path] = []
    for v in VARIANTS:
        vdir = root / v
        vdir.mkdir(parents=True, exist_ok=True)
        for f in FRAMES:
            p = vdir / f"{f}.png"
            # Unique content per file so sha8 is deterministic but distinct.
            p.write_bytes(f"PNG-FAKE:{v}:{f}".encode("utf-8"))
            paths.append(p)
    return sorted(paths, key=str)


def test_read_manifest_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "character.yaml"
    write_initial_manifest(path)
    data = read_manifest(path)
    assert data["schema_version"] == 1
    assert data["character_id"] == "forton-lab-mascot-v1"
    assert data["phase_8"]["status"] == "pending"
    assert data["phase_8"]["selected_variant"] is None
    assert data["lora"]["trigger_word"] == "OHWX_FORTONA"
    assert data["voice"]["provider"] == "elevenlabs"
    assert isinstance(data["history"], list) and data["history"]


def test_write_selection_happy_path(tmp_path: Path) -> None:
    yaml_path = tmp_path / "character.yaml"
    write_initial_manifest(yaml_path)
    frames = _make_frames(tmp_path / "frames")
    expected_sha8 = compute_batch_sha8(frames)

    result = write_selection(
        yaml_path,
        frame_root=tmp_path / "frames",
        selected="variant_2",
        batch_sha8=expected_sha8,
        character_card="card 2 text",
        total_spend_usd=0.30,
        selected_by="aleksey",
    )

    assert result["phase_8"]["status"] == "approved"
    assert result["phase_8"]["selected_variant"] == "variant_2"
    assert result["phase_8"]["batch_sha8"] == expected_sha8
    assert result["phase_8"]["character_card"] == "card 2 text"
    assert result["phase_8"]["total_spend_usd"] == 0.30

    reloaded = read_manifest(yaml_path)
    assert reloaded["phase_8"]["selected_variant"] == "variant_2"
    assert reloaded["phase_8"]["selected_at"] is not None
    assert "T" in reloaded["phase_8"]["selected_at"]  # ISO timestamp
    assert reloaded["updated_at"] is not None

    # history append: created + selected
    events = [h["event"] for h in reloaded["history"]]
    assert "created" in events
    assert "selected" in events


def test_write_selection_sha8_mismatch_aborts(tmp_path: Path) -> None:
    yaml_path = tmp_path / "character.yaml"
    write_initial_manifest(yaml_path)
    _make_frames(tmp_path / "frames")

    before = read_manifest(yaml_path)
    assert before["phase_8"]["status"] == "pending"

    with pytest.raises(SelectionMismatchError):
        write_selection(
            yaml_path,
            frame_root=tmp_path / "frames",
            selected="variant_2",
            batch_sha8="deadbeef",  # wrong
            character_card="card",
            total_spend_usd=0.30,
        )

    after = read_manifest(yaml_path)
    assert after["phase_8"]["status"] == "pending"
    assert after["phase_8"]["selected_variant"] is None
    assert after["phase_8"]["batch_sha8"] is None


def test_write_selection_invalid_variant(tmp_path: Path) -> None:
    yaml_path = tmp_path / "character.yaml"
    write_initial_manifest(yaml_path)
    frames = _make_frames(tmp_path / "frames")
    sha8 = compute_batch_sha8(frames)

    with pytest.raises(ValueError):
        write_selection(
            yaml_path,
            frame_root=tmp_path / "frames",
            selected="variant_9",
            batch_sha8=sha8,
            character_card="card",
            total_spend_usd=0.30,
        )

    after = read_manifest(yaml_path)
    assert after["phase_8"]["status"] == "pending"


def test_read_manifest_tolerates_absent_lora_voice(tmp_path: Path) -> None:
    """Phase 9/10 forward-compat: partial files don't crash read_manifest."""
    yaml_path = tmp_path / "character.yaml"
    minimal = textwrap.dedent(
        """\
        schema_version: 1
        character_id: forton-lab-mascot-v1
        brief:
          gender: female
        phase_8:
          status: pending
        """
    )
    yaml_path.write_text(minimal, encoding="utf-8")

    data = read_manifest(yaml_path)
    assert data["lora"]["trigger_word"] == "OHWX_FORTONA"
    assert data["lora"]["status"] == "pending"
    assert data["voice"]["provider"] == "elevenlabs"
    assert data["voice"]["language"] == "ru"
    assert data["history"] == []


def test_write_selection_missing_frames_raises(tmp_path: Path) -> None:
    """If frame_root has < 12 frames → SelectionMismatchError (anti-replay layer 2)."""
    yaml_path = tmp_path / "character.yaml"
    write_initial_manifest(yaml_path)
    (tmp_path / "frames" / "variant_1").mkdir(parents=True)
    (tmp_path / "frames" / "variant_1" / "closeup.png").write_bytes(b"x")

    with pytest.raises(SelectionMismatchError):
        write_selection(
            yaml_path,
            frame_root=tmp_path / "frames",
            selected="variant_1",
            batch_sha8="00000000",
            character_card="card",
            total_spend_usd=0.10,
        )


# ---------------------------------------------------------------------------
# Phase 9 Plan 03 — write_lora_ready tests
# ---------------------------------------------------------------------------


def _bootstrap_phase8_approved(tmp_path: Path) -> Path:
    """Bootstrap a manifest already past Phase 8 (selected_variant=variant_2)."""
    yaml_path = tmp_path / "character.yaml"
    write_initial_manifest(yaml_path)
    frames = _make_frames(tmp_path / "frames")
    sha8 = compute_batch_sha8(frames)
    write_selection(
        yaml_path,
        frame_root=tmp_path / "frames",
        selected="variant_2",
        batch_sha8=sha8,
        character_card="card-2",
        total_spend_usd=0.30,
        selected_by="tester",
    )
    return yaml_path


def test_write_lora_ready_happy_path(tmp_path: Path) -> None:
    yaml_path = _bootstrap_phase8_approved(tmp_path)

    result = write_lora_ready(
        yaml_path,
        model="x/forton-lab-character-v1",
        version_sha256="abc123def456",
        training_run_id="t_xyz",
        trigger_word="OHWX_FORTONA",
        training_dataset_size=30,
        training_cost_usd=2.18,
        dataset_path="ai_talent/dataset/v1",
        training_metadata={"steps": 1000, "rank": 16, "trainer_version": "def456"},
    )

    assert result["lora"]["status"] == "ready"
    assert result["lora"]["model"] == "x/forton-lab-character-v1"
    assert result["lora"]["version_sha256"] == "abc123def456"
    assert result["lora"]["trigger_word"] == "OHWX_FORTONA"
    assert result["lora"]["training_dataset_size"] == 30
    assert result["lora"]["training_run_id"] == "t_xyz"
    assert result["lora"]["training_cost_usd"] == 2.18
    assert result["lora"]["dataset_path"] == "ai_talent/dataset/v1"
    assert result["lora"]["training_metadata"] == {
        "steps": 1000,
        "rank": 16,
        "trainer_version": "def456",
    }

    reloaded = read_manifest(yaml_path)
    assert reloaded["lora"]["status"] == "ready"
    assert reloaded["lora"]["version_sha256"] == "abc123def456"

    events = [(h["phase"], h["event"]) for h in reloaded["history"]]
    assert (9, "lora_trained") in events
    last = reloaded["history"][-1]
    assert last["phase"] == 9
    assert last["event"] == "lora_trained"
    assert "x/forton-lab-character-v1:abc123def456" in last["note"]


def test_write_lora_ready_trigger_word_mismatch(tmp_path: Path) -> None:
    yaml_path = _bootstrap_phase8_approved(tmp_path)
    before = read_manifest(yaml_path)
    before_lora_status = before["lora"]["status"]
    before_history_len = len(before["history"])

    with pytest.raises(LoraTriggerMismatchError):
        write_lora_ready(
            yaml_path,
            model="x/forton-lab-character-v1",
            version_sha256="abc123def456",
            training_run_id="t_xyz",
            trigger_word="DIFFERENT",
            training_dataset_size=30,
            training_cost_usd=2.18,
            dataset_path="ai_talent/dataset/v1",
            training_metadata={"steps": 1000, "rank": 16, "trainer_version": "def456"},
        )

    after = read_manifest(yaml_path)
    assert after["lora"]["status"] == before_lora_status  # still pending
    assert after["lora"]["status"] == "pending"
    assert after["lora"]["model"] is None
    assert after["lora"]["version_sha256"] is None
    assert len(after["history"]) == before_history_len  # no event appended


def test_write_lora_ready_phase8_additivity(tmp_path: Path) -> None:
    """Phase 8 and voice blocks must remain byte-equal after lora write."""
    yaml_path = _bootstrap_phase8_approved(tmp_path)

    before = read_manifest(yaml_path)
    phase_8_before = yaml.safe_dump(before["phase_8"], sort_keys=False, allow_unicode=True)
    voice_before = yaml.safe_dump(before["voice"], sort_keys=False, allow_unicode=True)
    brief_before = yaml.safe_dump(before["brief"], sort_keys=False, allow_unicode=True)

    write_lora_ready(
        yaml_path,
        model="x/forton-lab-character-v1",
        version_sha256="abc123def456",
        training_run_id="t_xyz",
        trigger_word="OHWX_FORTONA",
        training_dataset_size=30,
        training_cost_usd=2.18,
        dataset_path="ai_talent/dataset/v1",
        training_metadata={"steps": 1000, "rank": 16, "trainer_version": "def456"},
    )

    after = read_manifest(yaml_path)
    phase_8_after = yaml.safe_dump(after["phase_8"], sort_keys=False, allow_unicode=True)
    voice_after = yaml.safe_dump(after["voice"], sort_keys=False, allow_unicode=True)
    brief_after = yaml.safe_dump(after["brief"], sort_keys=False, allow_unicode=True)

    assert phase_8_after == phase_8_before, "phase_8 block must be byte-equal"
    assert voice_after == voice_before, "voice block must be byte-equal"
    assert brief_after == brief_before, "brief block must be byte-equal"


def test_write_lora_ready_normalizes_full_ref(tmp_path: Path) -> None:
    """version_sha256 accepts `owner/name:sha` and stores only SHA part."""
    yaml_path = _bootstrap_phase8_approved(tmp_path)

    full_ref = (
        "carbon1777/forton-lab-character-v1:"
        "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1"
    )
    write_lora_ready(
        yaml_path,
        model="carbon1777/forton-lab-character-v1",
        version_sha256=full_ref,
        training_run_id="6pv1wkhrg9rmr0cy2fysg4y4e8",
        trigger_word="OHWX_FORTONA",
        training_dataset_size=30,
        training_cost_usd=2.20,
        dataset_path="ai_talent/dataset/v1",
        training_metadata={
            "steps": 1000,
            "rank": 16,
            "trainer_version": "26dce37af90b9d997eeb970d92e47de3064d46c300504ae376c75bef6a9022d2",
        },
    )

    reloaded = read_manifest(yaml_path)
    assert reloaded["lora"]["version_sha256"] == (
        "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1"
    )


def test_normalize_version_sha_helper() -> None:
    """Direct unit test for the SHA-normalization helper."""
    assert _normalize_version_sha("abc123def456") == "abc123def456"
    assert _normalize_version_sha("owner/name:abc123def456") == "abc123def456"
    assert (
        _normalize_version_sha(
            "carbon1777/forton-lab-character-v1:"
            "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1"
        )
        == "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1"
    )
    with pytest.raises(ValueError):
        _normalize_version_sha("not-a-sha")
    with pytest.raises(ValueError):
        _normalize_version_sha("owner/name:zzz")  # not hex
