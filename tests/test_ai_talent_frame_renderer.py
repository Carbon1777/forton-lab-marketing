"""Phase 11 Plan 05 — frame_renderer.py (Stage 2) tests.

Coverage:
    - resolve_lora_ref: yaml read + status gate (T-11-05-03 mitigation)
    - render_frame: trigger word validation, BOOT-01 ordering, Replicate API
      called with LoRA full_ref (not base flux-dev), predict_seconds unit_field
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.ai_talent import frame_renderer as fr
from src.ai_talent.frame_renderer import (
    COST_PER_FRAME_USD,
    PREDICT_SECONDS_PER_FRAME,
    TRIGGER_WORD,
    FrameRendererError,
    render_frame,
    resolve_lora_ref,
)


_LORA_VERSION = "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1"
_LORA_MODEL = "carbon1777/forton-lab-character-v1"
_LORA_FULL_REF = f"{_LORA_MODEL}:{_LORA_VERSION}"

# Minimal 1x1 PNG
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc"
    b"\x0f\x00\x00\x01\x01\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def yaml_with_lora_ready(tmp_path):
    p = tmp_path / "character.yaml"
    p.write_text(yaml.safe_dump({
        "lora": {
            "status": "ready",
            "model": _LORA_MODEL,
            "version_sha256": _LORA_VERSION,
            "trigger_word": TRIGGER_WORD,
        },
    }), encoding="utf-8")
    return p


@pytest.fixture
def yaml_with_lora_pending(tmp_path):
    p = tmp_path / "character.yaml"
    p.write_text(yaml.safe_dump({
        "lora": {"status": "pending", "model": _LORA_MODEL,
                 "version_sha256": _LORA_VERSION},
    }), encoding="utf-8")
    return p


@pytest.fixture
def mock_replicate_client():
    """replicate.Client mock — .run returns [FileOutput] with .read() -> bytes."""
    client = MagicMock()
    out_item = MagicMock()
    out_item.read.return_value = _PNG_1X1
    client.run.return_value = [out_item]
    return client


# --------------------------------------------------------------------------
# resolve_lora_ref
# --------------------------------------------------------------------------


def test_resolve_lora_ref_from_yaml(yaml_with_lora_ready):
    ref = resolve_lora_ref(yaml_with_lora_ready)
    assert ref == _LORA_FULL_REF


def test_resolve_lora_ref_rejects_when_not_ready(yaml_with_lora_pending):
    with pytest.raises(FrameRendererError, match="'ready'"):
        resolve_lora_ref(yaml_with_lora_pending)


def test_resolve_lora_ref_rejects_when_missing_file(tmp_path):
    with pytest.raises(FrameRendererError, match="character.yaml missing"):
        resolve_lora_ref(tmp_path / "nope.yaml")


def test_resolve_lora_ref_rejects_when_model_missing(tmp_path):
    p = tmp_path / "character.yaml"
    p.write_text(yaml.safe_dump({
        "lora": {"status": "ready", "version_sha256": _LORA_VERSION},
    }), encoding="utf-8")
    with pytest.raises(FrameRendererError, match="lora.model"):
        resolve_lora_ref(p)


# --------------------------------------------------------------------------
# render_frame
# --------------------------------------------------------------------------


def test_render_frame_rejects_when_lora_not_ready(
    yaml_with_lora_pending, mock_replicate_client, tmp_path, tmp_spend_file
):
    out = tmp_path / "frame.png"
    with pytest.raises(FrameRendererError, match="'ready'"):
        render_frame(
            f"{TRIGGER_WORD} test prompt",
            out_path=out,
            client=mock_replicate_client,
            char_yaml_path=yaml_with_lora_pending,
            spend_file=tmp_spend_file,
        )
    mock_replicate_client.run.assert_not_called()
    assert not out.exists()


def test_render_frame_rejects_when_prompt_missing_trigger(
    yaml_with_lora_ready, mock_replicate_client, tmp_path, tmp_spend_file
):
    out = tmp_path / "frame.png"
    with pytest.raises(FrameRendererError, match=TRIGGER_WORD):
        render_frame(
            "A woman smiling, no trigger word",
            out_path=out,
            client=mock_replicate_client,
            char_yaml_path=yaml_with_lora_ready,
            spend_file=tmp_spend_file,
        )
    mock_replicate_client.run.assert_not_called()


def test_render_frame_calls_replicate_with_lora_full_ref(
    yaml_with_lora_ready, mock_replicate_client, tmp_path, tmp_spend_file
):
    out = tmp_path / "frame.png"
    render_frame(
        f"{TRIGGER_WORD} gentle smile, warm light",
        out_path=out,
        client=mock_replicate_client,
        char_yaml_path=yaml_with_lora_ready,
        spend_file=tmp_spend_file,
    )
    args, kwargs = mock_replicate_client.run.call_args
    # First positional arg = model:version full ref (NOT base flux-dev)
    assert args[0] == _LORA_FULL_REF
    assert _LORA_VERSION in args[0]
    assert "flux-dev" not in args[0]
    # 9:16 aspect ratio
    assert kwargs["input"]["aspect_ratio"] == "9:16"


def test_render_frame_writes_bytes_to_out_path(
    yaml_with_lora_ready, mock_replicate_client, tmp_path, tmp_spend_file
):
    out = tmp_path / "subdir" / "frame.png"
    render_frame(
        f"{TRIGGER_WORD} test",
        out_path=out,
        client=mock_replicate_client,
        char_yaml_path=yaml_with_lora_ready,
        spend_file=tmp_spend_file,
    )
    assert out.exists()
    assert out.read_bytes() == _PNG_1X1


def test_render_frame_records_predict_seconds_units(
    yaml_with_lora_ready, mock_replicate_client, tmp_path, tmp_spend_file
):
    out = tmp_path / "frame.png"
    render_frame(
        f"{TRIGGER_WORD} test",
        out_path=out,
        client=mock_replicate_client,
        char_yaml_path=yaml_with_lora_ready,
        spend_file=tmp_spend_file,
    )
    spend = json.loads(tmp_spend_file.read_text())
    month_keys = [k for k in spend if k[:4].isdigit() and "-" in k]
    assert month_keys
    month = spend[month_keys[0]]
    replicate_entry = month["by_provider"]["replicate"]
    assert replicate_entry["predict_seconds"] == PREDICT_SECONDS_PER_FRAME
    assert replicate_entry["usd"] == pytest.approx(COST_PER_FRAME_USD)


def test_render_frame_BOOT_01_ordering(
    yaml_with_lora_ready, mock_replicate_client, tmp_path, tmp_spend_file, mocker
):
    """preflight runs before replicate.run; record_spend runs after file write."""
    preflight = mocker.patch("src.ai_talent.frame_renderer.preflight_check")
    record = mocker.patch("src.ai_talent.frame_renderer.record_provider_spend")
    call_order: list[str] = []
    preflight.side_effect = lambda *a, **kw: call_order.append("preflight")

    def fake_run(*a, **kw):
        call_order.append("run")
        return mock_replicate_client.run.return_value

    mock_replicate_client.run.side_effect = fake_run
    record.side_effect = lambda *a, **kw: call_order.append("record")

    out = tmp_path / "frame.png"
    render_frame(
        f"{TRIGGER_WORD} test",
        out_path=out,
        client=mock_replicate_client,
        char_yaml_path=yaml_with_lora_ready,
        spend_file=tmp_spend_file,
    )
    assert call_order == ["preflight", "run", "record"]
    assert out.exists()


def test_render_frame_passes_seed_when_given(
    yaml_with_lora_ready, mock_replicate_client, tmp_path, tmp_spend_file
):
    out = tmp_path / "frame.png"
    render_frame(
        f"{TRIGGER_WORD} test",
        out_path=out,
        client=mock_replicate_client,
        char_yaml_path=yaml_with_lora_ready,
        spend_file=tmp_spend_file,
        seed=42,
    )
    kwargs = mock_replicate_client.run.call_args.kwargs
    assert kwargs["input"]["seed"] == 42


def test_render_frame_supports_single_file_output(
    yaml_with_lora_ready, tmp_path, tmp_spend_file
):
    """Newer Replicate SDK may return single FileOutput, not list."""
    client = MagicMock()
    out_item = MagicMock()
    out_item.read.return_value = _PNG_1X1
    # Has .read() directly (not a list)
    client.run.return_value = out_item

    out = tmp_path / "frame.png"
    render_frame(
        f"{TRIGGER_WORD} test",
        out_path=out,
        client=client,
        char_yaml_path=yaml_with_lora_ready,
        spend_file=tmp_spend_file,
    )
    assert out.read_bytes() == _PNG_1X1
