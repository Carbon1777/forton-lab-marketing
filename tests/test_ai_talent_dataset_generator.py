"""Tests for Phase 9 Plan 01 — dataset_generator (Replicate Flux dev variation matrix).

Covers:
  - Variation-matrix balance (≥6 per frame_type bucket).
  - Caption format (trigger word + composition only, no identifying features).
  - BOOT-01 invariant: preflight_check → client.run → write_bytes → record_provider_spend.
  - provider_monthly_cap override = $6 (Phase 9 default $4 insufficient).
  - JPG-only output (no WebP/PNG — ostris trainer safety).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ai_talent import dataset_generator as dg


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
# Reuse the existing mock binary — png bytes are fine for testing write path,
# the validator checks the filename suffix, not the bytes content.
MOCK_BYTES = (FIXTURES_DIR / "mock_replicate_output.png").read_bytes()


class FakeFileOutput:
    """Mimics replicate.helpers.FileOutput — exposes .read() returning bytes."""

    def __init__(self, b: bytes) -> None:
        self._b = b

    def read(self) -> bytes:
        return self._b


def _make_fake_client() -> MagicMock:
    client = MagicMock()
    client.run.side_effect = lambda *a, **kw: [FakeFileOutput(MOCK_BYTES)]
    return client


@pytest.fixture
def spend_file(tmp_path: Path) -> Path:
    metrics = tmp_path / ".metrics"
    metrics.mkdir()
    sf = metrics / "api_spend.json"
    sf.write_text(json.dumps({"_schema_version": 3, "_updated": None}), encoding="utf-8")
    return sf


# ---------------------------------------------------------------------------
# Test 1 — Variation matrix balance (CHAR-03 + 09-RESEARCH §Pitfall 1)
# ---------------------------------------------------------------------------
def test_variation_matrix_balanced() -> None:
    assert len(dg.COMPOSITIONS_30) == 30, (
        f"Expected exactly 30 entries; got {len(dg.COMPOSITIONS_30)}"
    )
    buckets: dict[str, int] = {
        "close-up": 0,
        "medium": 0,
        "3/4 body": 0,
        "full body": 0,
        "lifestyle": 0,
    }
    for frame_type, _angle, _expression in dg.COMPOSITIONS_30:
        ft_lower = frame_type.lower()
        for bucket in buckets:
            if ft_lower.startswith(bucket):
                buckets[bucket] += 1
                break
    for bucket, count in buckets.items():
        assert count >= 6, f"Bucket '{bucket}' has {count} entries (need ≥6)"


# ---------------------------------------------------------------------------
# Test 2 — Caption format: no identifying features (09-RESEARCH §Pitfall 2)
# ---------------------------------------------------------------------------
def test_caption_format_no_identifying_features() -> None:
    forbidden = re.compile(
        r"\b(brunette|brown hair|blue eyes|26[- ]year|russian|woman)\b",
        re.IGNORECASE,
    )
    for frame_type, angle, expression in dg.COMPOSITIONS_30:
        caption = dg.build_caption(frame_type, angle, expression)
        assert caption.startswith("OHWX_FORTONA, "), (
            f"Caption missing trigger word prefix: {caption!r}"
        )
        assert not forbidden.search(caption), (
            f"Caption contains identifying feature: {caption!r}"
        )


# ---------------------------------------------------------------------------
# Test 3 — BOOT-01 invariant: preflight → run → write → record
# ---------------------------------------------------------------------------
def test_generate_frame_boot01_ordering(tmp_path: Path, spend_file: Path) -> None:
    client = _make_fake_client()
    call_order: list[str] = []

    real_write_bytes = Path.write_bytes

    def tracked_write_bytes(self: Path, data: bytes) -> int:
        if self.suffix == ".jpg":
            call_order.append("write_bytes")
        return real_write_bytes(self, data)

    with patch.object(dg, "preflight_check") as mock_pre, \
         patch.object(dg, "record_provider_spend") as mock_rec, \
         patch.object(Path, "write_bytes", tracked_write_bytes):
        mock_pre.side_effect = lambda *a, **kw: call_order.append("preflight_check")
        mock_rec.side_effect = lambda *a, **kw: call_order.append("record_provider_spend")
        client.run.side_effect = lambda *a, **kw: (
            call_order.append("client.run") or [FakeFileOutput(MOCK_BYTES)]
        )

        dg.generate_frame(
            client=client,
            card="A 26-year-old test card text",
            index=1,
            out_dir=tmp_path / "dataset",
            spend_file=spend_file,
        )

    assert call_order == [
        "preflight_check",
        "client.run",
        "write_bytes",
        "record_provider_spend",
    ], f"Wrong BOOT-01 ordering: {call_order}"


# ---------------------------------------------------------------------------
# Test 4 — provider_monthly_cap override = $6
# ---------------------------------------------------------------------------
def test_provider_monthly_cap_override(tmp_path: Path, spend_file: Path) -> None:
    client = _make_fake_client()
    with patch.object(dg, "preflight_check") as mock_pre, \
         patch.object(dg, "record_provider_spend"):
        dg.generate_frame(
            client=client,
            card="card",
            index=2,
            out_dir=tmp_path / "dataset",
            spend_file=spend_file,
        )
        assert mock_pre.call_count == 1
        kwargs = mock_pre.call_args.kwargs
        assert kwargs.get("provider_monthly_cap") == 6.0, (
            f"Expected provider_monthly_cap=6.0; got {kwargs!r}"
        )


# ---------------------------------------------------------------------------
# Test 5 — JPG-only output (no WebP, no PNG)
# ---------------------------------------------------------------------------
def test_validate_output_path_jpg_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        dg._validate_output_path(tmp_path / "foo.webp")
    with pytest.raises(ValueError):
        dg._validate_output_path(tmp_path / "foo.png")
    # JPG should pass
    dg._validate_output_path(tmp_path / "foo.jpg")


# ---------------------------------------------------------------------------
# Test 6 — Trigger word consistency with character.yaml
# ---------------------------------------------------------------------------
def test_trigger_word_locked_constant() -> None:
    assert dg.TRIGGER_WORD_LOCKED == "OHWX_FORTONA"
    # build_caption uses the constant
    cap = dg.build_caption("close-up portrait", "front", "neutral")
    assert "OHWX_FORTONA" in cap
