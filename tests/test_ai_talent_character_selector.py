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
    SelectionMismatchError,
    read_manifest,
    write_initial_manifest,
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
