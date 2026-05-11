"""Tests for src.ai_talent._ltx_api — LTX client (PIPE-05).

No live LTX API calls — requests.post is monkeypatched in every test.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------- estimate_cost ----------

def test_estimate_cost_baseline_1080():
    from src.ai_talent._ltx_api import estimate_cost
    assert estimate_cost("ltx-2-3-pro", 5, "1080x1920") == pytest.approx(0.40)


def test_estimate_cost_1440_multiplier():
    from src.ai_talent._ltx_api import estimate_cost
    assert estimate_cost("ltx-2-3-pro", 5, "1440x2560") == pytest.approx(0.80)


def test_estimate_cost_2160_multiplier():
    from src.ai_talent._ltx_api import estimate_cost
    assert estimate_cost("ltx-2-3-pro", 5, "2160x3840") == pytest.approx(1.60)


def test_estimate_cost_unknown_model_raises():
    from src.ai_talent._ltx_api import LtxError, estimate_cost
    with pytest.raises((LtxError, KeyError)):
        estimate_cost("ltx-99", 5, "1080x1920")


# ---------- _read_key ----------

def test_read_key_from_env(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test_abc123")
    assert _ltx_api._read_key() == "ltxv_test_abc123"


def test_read_key_invalid_prefix_raises(monkeypatch, tmp_path):
    from src.ai_talent import _ltx_api
    from src.ai_talent._ltx_api import LtxAuthError, _read_key
    monkeypatch.setenv("LTX_API_KEY", "garbage_no_prefix")
    # Also ensure .env.ltx fallback isn't there
    monkeypatch.setattr(_ltx_api, "DEFAULT_ENV_LTX", tmp_path / "nonexistent.env.ltx")
    with pytest.raises(LtxAuthError, match="(?i)ltxv_|prefix"):
        _read_key()


def test_read_key_missing_env_and_file_raises(monkeypatch, tmp_path):
    from src.ai_talent import _ltx_api
    from src.ai_talent._ltx_api import LtxAuthError, _read_key
    monkeypatch.delenv("LTX_API_KEY", raising=False)
    monkeypatch.setattr(_ltx_api, "DEFAULT_ENV_LTX", tmp_path / "nonexistent.env.ltx")
    with pytest.raises(LtxAuthError):
        _read_key()


def test_read_key_from_env_file_fallback(monkeypatch, tmp_path):
    from src.ai_talent import _ltx_api
    from src.ai_talent._ltx_api import _read_key
    monkeypatch.delenv("LTX_API_KEY", raising=False)
    env_file = tmp_path / ".env.ltx"
    env_file.write_text("ltxv_file_token_xyz\n", encoding="utf-8")
    monkeypatch.setattr(_ltx_api, "DEFAULT_ENV_LTX", env_file)
    assert _read_key() == "ltxv_file_token_xyz"


# ---------- generate ----------

def test_generate_success_returns_mp4_bytes(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test_xyz")

    fake_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
    fake_resp = MagicMock(status_code=200, content=fake_mp4)
    fake_resp.raise_for_status = lambda: None

    with patch.object(_ltx_api.requests, "post", return_value=fake_resp) as post_mock:
        result = _ltx_api.generate(
            prompt="OHWX_FORTONA close-up gentle smile",
            duration_sec=5,
            model="ltx-2-3-pro",
            resolution="1080x1920",
        )
    assert isinstance(result, (bytes, bytearray))
    assert result == fake_mp4
    # Confirm the URL was hit
    assert post_mock.call_args[0][0] == "https://api.ltx.video/v1/text-to-video"


def test_generate_401_raises_auth_error(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    fake_resp = MagicMock(status_code=401, text="unauthorized")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp):
        with pytest.raises(_ltx_api.LtxAuthError):
            _ltx_api.generate(prompt="x", duration_sec=5)


def test_generate_429_raises_quota_error(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    fake_resp = MagicMock(status_code=429, text="rate limited")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp):
        with pytest.raises(_ltx_api.LtxQuotaError):
            _ltx_api.generate(prompt="x", duration_sec=5)


def test_generate_402_raises_quota_error(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    fake_resp = MagicMock(status_code=402, text="payment required")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp):
        with pytest.raises(_ltx_api.LtxQuotaError):
            _ltx_api.generate(prompt="x", duration_sec=5)


def test_generate_500_raises_generic_error(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    fake_resp = MagicMock(status_code=500, text="server error")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp):
        with pytest.raises(_ltx_api.LtxError):
            _ltx_api.generate(prompt="x", duration_sec=5)


def test_generate_invalid_model_raises(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    with pytest.raises(_ltx_api.LtxError):
        _ltx_api.generate(prompt="x", duration_sec=5, model="bogus-model")


def test_generate_request_body_shape(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    fake_resp = MagicMock(status_code=200, content=b"mp4bytes")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp) as post:
        _ltx_api.generate(
            prompt="test prompt",
            duration_sec=5,
            model="ltx-2-3-pro",
            resolution="1080x1920",
            fps=24,
        )
    body = post.call_args[1]["data"]
    parsed = json.loads(body)
    assert parsed["prompt"] == "test prompt"
    assert parsed["model"] == "ltx-2-3-pro"
    assert parsed["duration"] == 5
    assert parsed["resolution"] == "1080x1920"
    assert parsed["fps"] == 24
    assert parsed["generate_audio"] is False
    headers = post.call_args[1]["headers"]
    assert headers["Authorization"].startswith("Bearer ltxv_")
    assert headers["Content-Type"] == "application/json"


# ---------- image_path (Q-LTX-IMG resolved YES) ----------

def test_generate_with_image_path_encodes_base64(monkeypatch, tmp_path):
    """Q-LTX-IMG resolved 2026-05-11: API accepts and USES image_base64 conditioning."""
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")

    img_bytes = b"\x89PNG\r\n\x1a\nfake-png-data-for-test"
    img_path = tmp_path / "reference.png"
    img_path.write_bytes(img_bytes)

    fake_resp = MagicMock(status_code=200, content=b"mp4")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp) as post:
        _ltx_api.generate(
            prompt="OHWX_FORTONA smile",
            duration_sec=5,
            image_path=img_path,
        )

    body = post.call_args[1]["data"]
    parsed = json.loads(body)
    assert "image_base64" in parsed, "image_path should encode to image_base64 in body"
    import base64
    assert base64.b64decode(parsed["image_base64"]) == img_bytes


def test_generate_without_image_path_omits_field(monkeypatch):
    from src.ai_talent import _ltx_api
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test")
    fake_resp = MagicMock(status_code=200, content=b"mp4")
    with patch.object(_ltx_api.requests, "post", return_value=fake_resp) as post:
        _ltx_api.generate(prompt="test", duration_sec=5)
    body = post.call_args[1]["data"]
    parsed = json.loads(body)
    assert "image_base64" not in parsed
