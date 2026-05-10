"""Phase 9-05 — identity anchor tests (Facenet512 + cosine ≥0.85, PITFALLS P7 / CHAR-06).

Real DeepFace tests — cold-start downloads weights (~215 MB), subsequent runs ~1-2s/face.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.ai_talent.identity_anchor import (
    DEFAULT_THRESHOLD,
    MODEL_NAME,
    DETECTOR_BACKEND,
    _get_embedding,
    cosine_similarity,
    freeze_v1,
    verify_identity,
)


FIXTURES = Path(__file__).parent / "fixtures" / "identity"
V1_SELF = FIXTURES / "v1_self.png"
V2_DUMMY = FIXTURES / "v2_dummy.png"


@pytest.fixture(scope="module")
def v1_embedding() -> list[float]:
    """Cache one real embedding across tests to skip duplicate model runs."""
    return _get_embedding(V1_SELF)


def test_embedding_shape(v1_embedding: list[float]) -> None:
    """Facenet512 returns 512-dim embedding."""
    assert isinstance(v1_embedding, list)
    assert len(v1_embedding) == 512
    assert all(isinstance(x, float) for x in v1_embedding)


def test_self_cosine_is_one(v1_embedding: list[float]) -> None:
    """cosine(emb, emb) == 1.0 by definition."""
    sim = cosine_similarity(v1_embedding, v1_embedding)
    assert sim == pytest.approx(1.0, abs=1e-6)


def test_freeze_then_verify_dummy_v2(tmp_path: Path) -> None:
    """End-to-end: freeze on 5 PNGs, verify dummy-v2 (brightness 0.95) cosine ≥0.85."""
    smoke_dir = tmp_path / "smoke"
    smoke_dir.mkdir()
    for i, name in enumerate(
        ["01_closeup.png", "02_three_quarter.png", "03_fullbody.png",
         "04_profile.png", "05_emotion.png"],
        start=1,
    ):
        shutil.copy2(V1_SELF, smoke_dir / name)

    anchor_dir = tmp_path / "anchor"
    lora_ref = {"model": "x/m", "version_sha256": "deadbeef"}
    out = freeze_v1(smoke_dir=smoke_dir, anchor_dir=anchor_dir, character_yaml_lora=lora_ref)

    assert out.exists()
    data = json.loads(out.read_text())
    assert data["model"] == MODEL_NAME
    assert data["detector"] == DETECTOR_BACKEND
    assert data["threshold"] == DEFAULT_THRESHOLD
    assert data["character_yaml_lora"] == lora_ref
    assert len(data["embeddings"]) == 5
    for fname, emb in data["embeddings"].items():
        assert len(emb) == 512, f"embedding {fname} not 512-dim"
    assert len(data["mean_embedding"]) == 512

    # mechanism: slightly-modified copy should pass cosine ≥0.85
    ok = verify_identity(V2_DUMMY, anchor_path=out)
    assert ok, "verify_identity must accept brightness-shifted v2 dummy"


def test_cosine_similarity_zero_vector() -> None:
    """Zero-norm vector returns 0.0 instead of NaN."""
    assert cosine_similarity([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0
