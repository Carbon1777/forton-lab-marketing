"""Tests for Phase 9 Plan 04 — smoke_test (5 LOCKED prompts vs trained LoRA).

Covers:
  - SMOKE_PROMPTS is locked as anchor fixture (5 entries, OHWX_FORTONA, exact names).
  - run_smoke refuses when character.yaml.lora.status != 'ready'.
  - BOOT-01 invariant per frame: preflight → client.run → write → record (per iteration).
  - build_collage produces a 1x5 horizontal PNG of expected dimensions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from PIL import Image

from src.ai_talent import smoke_test as st


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MOCK_PNG_BYTES = (FIXTURES_DIR / "mock_replicate_output.png").read_bytes()


class FakeFileOutput:
    """Mimics replicate.helpers.FileOutput — exposes .read() returning bytes."""

    def __init__(self, b: bytes) -> None:
        self._b = b

    def read(self) -> bytes:
        return self._b


@pytest.fixture
def spend_file(tmp_path: Path) -> Path:
    metrics = tmp_path / ".metrics"
    metrics.mkdir()
    sf = metrics / "api_spend.json"
    sf.write_text(json.dumps({"_schema_version": 3, "_updated": None}), encoding="utf-8")
    return sf


@pytest.fixture
def ready_yaml(tmp_path: Path) -> Path:
    """character.yaml with lora.status=ready — used by happy-path tests."""
    data = {
        "schema_version": 1,
        "character_id": "test-character",
        "lora": {
            "status": "ready",
            "model": "carbon1777/forton-lab-character-v1",
            "version_sha256": "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1",
            "trigger_word": "OHWX_FORTONA",
        },
    }
    p = tmp_path / "character.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Test 1 — SMOKE_PROMPTS is locked as anchor fixture
# ---------------------------------------------------------------------------
def test_smoke_prompts_locked() -> None:
    """5 entries, exact names, every prompt starts with OHWX_FORTONA trigger."""
    assert len(st.SMOKE_PROMPTS) == 5

    expected_names = [
        "01_closeup",
        "02_three_quarter",
        "03_fullbody",
        "04_profile",
        "05_emotion",
    ]
    actual_names = [name for name, _ in st.SMOKE_PROMPTS]
    assert actual_names == expected_names

    for name, prompt in st.SMOKE_PROMPTS:
        assert prompt.startswith("OHWX_FORTONA, "), (
            f"{name}: prompt must start with 'OHWX_FORTONA, ' — got: {prompt[:40]!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — pre-flight refusal when lora.status != ready
# ---------------------------------------------------------------------------
def test_run_smoke_refuses_when_lora_not_ready(tmp_path: Path, spend_file: Path) -> None:
    data = {
        "schema_version": 1,
        "lora": {
            "status": "pending",
            "model": "carbon1777/forton-lab-character-v1",
            "version_sha256": "abc",
        },
    }
    yaml_path = tmp_path / "character.yaml"
    yaml_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    client = MagicMock()
    with pytest.raises(RuntimeError, match="lora.status"):
        st.run_smoke(
            yaml_path,
            out_dir=tmp_path / "smoke",
            spend_file=spend_file,
            client=client,
        )
    assert client.run.call_count == 0


# ---------------------------------------------------------------------------
# Test 3 — BOOT-01 invariant ordering per frame iteration
# ---------------------------------------------------------------------------
def test_run_smoke_boot01_ordering(
    tmp_path: Path,
    spend_file: Path,
    ready_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For each of 5 prompts: preflight → run → record (in that order)."""
    order: list[str] = []

    def fake_preflight(*args, **kwargs):
        order.append("preflight")

    def fake_record(*args, **kwargs):
        order.append("record")

    client = MagicMock()

    def fake_run(*args, **kwargs):
        order.append("run")
        return [FakeFileOutput(MOCK_PNG_BYTES)]

    client.run.side_effect = fake_run
    monkeypatch.setattr(st, "preflight_check", fake_preflight)
    monkeypatch.setattr(st, "record_provider_spend", fake_record)

    out_dir = tmp_path / "smoke"
    paths = st.run_smoke(ready_yaml, out_dir=out_dir, spend_file=spend_file, client=client)

    # 5 frames produced
    assert len(paths) == 5
    assert all(p.exists() for p in paths)

    # 5 × (preflight, run, record) — each iteration in canonical order
    assert order == ["preflight", "run", "record"] * 5

    # anchor_prompts.txt + collage written
    assert (out_dir / "anchor_prompts.txt").exists()
    assert (out_dir / "collage_1x5.png").exists()


# ---------------------------------------------------------------------------
# Test 4 — replicate.run is called with full_ref from character.yaml
# ---------------------------------------------------------------------------
def test_run_smoke_uses_full_ref_from_yaml(
    tmp_path: Path,
    spend_file: Path,
    ready_yaml: Path,
) -> None:
    captured: list = []
    client = MagicMock()

    def fake_run(*args, **kwargs):
        captured.append((args, kwargs))
        return [FakeFileOutput(MOCK_PNG_BYTES)]

    client.run.side_effect = fake_run

    st.run_smoke(ready_yaml, out_dir=tmp_path / "smoke", spend_file=spend_file, client=client)

    # First positional arg = full_ref "model:version_sha256"
    assert client.run.call_count == 5
    for args, kwargs in captured:
        assert args[0] == (
            "carbon1777/forton-lab-character-v1:"
            "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1"
        )
        # input shape — 9:16 png with 28 steps + guidance 3.5
        inp = kwargs["input"]
        assert inp["aspect_ratio"] == "9:16"
        assert inp["output_format"] == "png"
        assert inp["num_outputs"] == 1
        assert inp["num_inference_steps"] == 28
        assert inp["guidance_scale"] == 3.5
        # prompt is one of the 5 LOCKED prompts
        assert inp["prompt"].startswith("OHWX_FORTONA, ")


# ---------------------------------------------------------------------------
# Test 5 — collage builder produces 1x5 horizontal PNG
# ---------------------------------------------------------------------------
def test_build_collage_1x5_dimensions(tmp_path: Path) -> None:
    """Build collage from 5 same-size source images; verify width = 5×thumb + gaps."""
    src_paths: list[Path] = []
    src_w, src_h = 768, 1365  # ~9:16 source size
    for i in range(5):
        img = Image.new("RGB", (src_w, src_h), (40 + i * 30, 20, 8))
        p = tmp_path / f"src_{i}.png"
        img.save(p, "PNG")
        src_paths.append(p)

    out_path = tmp_path / "collage_1x5.png"
    thumb_h = 768
    st.build_collage(src_paths, out_path, thumb_h=thumb_h)

    assert out_path.exists()
    canvas = Image.open(out_path)

    # Each thumb width = round(src_w * thumb_h / src_h)
    expected_thumb_w = int(src_w * thumb_h / src_h)
    expected_width = 5 * expected_thumb_w + 4 * 8  # 8px gaps × 4

    # Allow ±10px tolerance for rounding inside Pillow resample
    assert abs(canvas.width - expected_width) <= 10
    assert canvas.height == thumb_h


# ---------------------------------------------------------------------------
# Test 6 — anchor_prompts.txt writer
# ---------------------------------------------------------------------------
def test_write_anchor_prompts(tmp_path: Path) -> None:
    out_dir = tmp_path / "smoke"
    path = st.write_anchor_prompts(out_dir)
    assert path == out_dir / "anchor_prompts.txt"

    lines = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        name, prompt = line.split("\t", 1)
        assert name == st.SMOKE_PROMPTS[i][0]
        assert prompt == st.SMOKE_PROMPTS[i][1]
        assert "OHWX_FORTONA" in prompt
