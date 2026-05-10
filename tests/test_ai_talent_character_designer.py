"""Tests for Phase 8 Plan 01 — character_designer (Replicate Flux dev wrapper).

Covers BOOT-01 invariant: every replicate.run() call is preceded by
preflight_check and followed by record_provider_spend.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ai_talent import character_designer as cd
from src.spend_tracker_v2 import ProviderMonthlyCapExceededError

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MOCK_PNG_BYTES = (FIXTURES_DIR / "mock_replicate_output.png").read_bytes()


class FakeFileOutput:
    """Mimics replicate.helpers.FileOutput — exposes .read() returning bytes."""

    def __init__(self, b: bytes) -> None:
        self._b = b

    def read(self) -> bytes:
        return self._b


def _make_fake_client(captured: list | None = None) -> MagicMock:
    """Build a MagicMock that mimics replicate.Client.

    If `captured` list is provided, each call to .run() appends (args, kwargs)
    so tests can assert input shape.
    """
    client = MagicMock()

    def fake_run(*args, **kwargs):
        if captured is not None:
            captured.append((args, kwargs))
        return [FakeFileOutput(MOCK_PNG_BYTES)]

    client.run.side_effect = fake_run
    return client


@pytest.fixture
def spend_file(tmp_path: Path) -> Path:
    """Fresh empty spend tracker JSON in tmp_path."""
    metrics = tmp_path / ".metrics"
    metrics.mkdir()
    sf = metrics / "api_spend.json"
    sf.write_text(json.dumps({"_schema_version": 3, "_updated": None}), encoding="utf-8")
    return sf


# ---------------------------------------------------------------------------
# Test 1 — happy path: one frame written, spend recorded once
# ---------------------------------------------------------------------------
def test_generate_frame_happy_path(tmp_path: Path, spend_file: Path) -> None:
    client = _make_fake_client()
    out_path = tmp_path / ".cache" / "character_preview" / "v1" / "variant_1" / "closeup.png"

    result = cd.generate_frame(
        client=client,
        card="A confident host in dark jacket, brown eyes",
        composition_name="closeup",
        out_path=out_path,
        spend_file=spend_file,
    )

    assert result == out_path
    assert out_path.exists()
    assert out_path.read_bytes() == MOCK_PNG_BYTES
    assert client.run.call_count == 1

    # Spend recorded with predict_seconds field
    data = json.loads(spend_file.read_text(encoding="utf-8"))
    months = [k for k in data.keys() if not k.startswith("_")]
    assert len(months) == 1
    bp = data[months[0]]["by_provider"]
    assert "replicate" in bp
    assert bp["replicate"]["calls"] == 1
    assert bp["replicate"]["usd"] == pytest.approx(cd.COST_PER_FRAME_USD)
    assert bp["replicate"]["predict_seconds"] == cd.PREDICT_SECONDS_PER_FRAME


# ---------------------------------------------------------------------------
# Test 2 — preflight called BEFORE replicate.run
# ---------------------------------------------------------------------------
def test_preflight_called_before_run(tmp_path: Path, spend_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    client = MagicMock()

    def fake_preflight(*args, **kwargs):
        order.append("preflight")

    def fake_run(*args, **kwargs):
        order.append("run")
        return [FakeFileOutput(MOCK_PNG_BYTES)]

    def fake_record(*args, **kwargs):
        order.append("record")

    client.run.side_effect = fake_run
    monkeypatch.setattr(cd, "preflight_check", fake_preflight)
    monkeypatch.setattr(cd, "record_provider_spend", fake_record)

    cd.generate_frame(
        client=client,
        card="card",
        composition_name="medium",
        out_path=tmp_path / "out.png",
        spend_file=spend_file,
    )

    assert order == ["preflight", "run", "record"]


# ---------------------------------------------------------------------------
# Test 3 — preflight cap blocks run; record NOT called
# ---------------------------------------------------------------------------
def test_preflight_cap_blocks_run(tmp_path: Path, spend_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    record_calls: list = []

    def boom(*args, **kwargs):
        raise ProviderMonthlyCapExceededError("replicate cap exceeded")

    def fake_record(*args, **kwargs):
        record_calls.append(args)

    monkeypatch.setattr(cd, "preflight_check", boom)
    monkeypatch.setattr(cd, "record_provider_spend", fake_record)

    with pytest.raises(ProviderMonthlyCapExceededError):
        cd.generate_frame(
            client=client,
            card="card",
            composition_name="closeup",
            out_path=tmp_path / "out.png",
            spend_file=spend_file,
        )

    assert client.run.call_count == 0, "replicate.run must NOT be called when preflight raises"
    assert record_calls == [], "record_provider_spend must NOT be called when preflight raises"
    assert not (tmp_path / "out.png").exists()


# ---------------------------------------------------------------------------
# Test 4 — missing REPLICATE_API_TOKEN raises CharacterDesignError
# ---------------------------------------------------------------------------
def test_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    with pytest.raises(cd.CharacterDesignError) as excinfo:
        cd._make_client()
    assert "REPLICATE_API_TOKEN" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Test 5 — generate_batch produces exactly 12 PNGs, 12 spend records
# ---------------------------------------------------------------------------
def test_generate_batch_12_files(tmp_path: Path, spend_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_fake_client()
    record_calls: list = []

    real_record = cd.record_provider_spend

    def counting_record(*args, **kwargs):
        record_calls.append((args, kwargs))
        real_record(*args, **kwargs)

    monkeypatch.setattr(cd, "record_provider_spend", counting_record)

    cards = {
        "variant_1": "Character A: warm, scholarly, late-20s",
        "variant_2": "Character B: bold, energetic, early-30s",
        "variant_3": "Character C: introspective, gentle, mid-30s",
    }
    out_dir = tmp_path / "character_preview" / "v1"

    result = cd.generate_batch(cards, out_dir=out_dir, spend_file=spend_file, client=client)

    # Shape: 3 variants, 4 frames each
    assert set(result.keys()) == {"variant_1", "variant_2", "variant_3"}
    for variant_id in ("variant_1", "variant_2", "variant_3"):
        for comp in ("closeup", "medium", "fullbody", "lifestyle"):
            p = out_dir / variant_id / f"{comp}.png"
            assert p.exists(), f"missing frame: {p}"
            assert p.read_bytes() == MOCK_PNG_BYTES

    # 12 disk files
    pngs = list(out_dir.rglob("*.png"))
    assert len(pngs) == 12, f"expected 12 PNGs, got {len(pngs)}"

    # 12 replicate.run calls + 12 spend records
    assert client.run.call_count == 12
    assert len(record_calls) == 12
    # Every record call uses provider="replicate" and unit_field="predict_seconds"
    for args, kwargs in record_calls:
        assert args[1] == "replicate"
        assert kwargs.get("unit_field") == "predict_seconds"


# ---------------------------------------------------------------------------
# Test 6 — replicate.run input shape: aspect_ratio, output_format, seed, prompt
# ---------------------------------------------------------------------------
def test_replicate_input_shape(tmp_path: Path, spend_file: Path) -> None:
    captured: list = []
    client = _make_fake_client(captured=captured)

    card = "A specific character description"

    for comp in ("closeup", "medium", "fullbody", "lifestyle"):
        cd.generate_frame(
            client=client,
            card=card,
            composition_name=comp,
            out_path=tmp_path / f"{comp}.png",
            spend_file=spend_file,
        )

    assert len(captured) == 4
    seen_compositions = set()
    for args, kwargs in captured:
        # model ref positional
        assert args[0] == cd.MODEL_REF
        inp = kwargs["input"]
        assert inp["aspect_ratio"] == "9:16"
        assert inp["output_format"] == "png"
        assert inp["num_outputs"] == 1
        assert isinstance(inp["seed"], int)
        assert 0 <= inp["seed"] <= 0x7FFFFFFF
        # prompt contains card text
        assert card in inp["prompt"]
        # prompt contains exactly ONE composition directive — identify which
        matched = [
            name for name, directive in cd.COMPOSITIONS.items()
            if directive in inp["prompt"]
        ]
        assert len(matched) == 1, f"prompt should contain exactly 1 composition directive, got {matched}"
        seen_compositions.add(matched[0])

    # All four compositions covered
    assert seen_compositions == set(cd.COMPOSITIONS.keys())
