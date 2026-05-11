"""Phase 11-06 — preflight.py (PIPE-04) unit tests.

5 health checks: replicate, elevenlabs, ltx, spend_tracker, character_yaml.
Each check returns (bool, msg). CLI exits 0 GREEN / 1 RED. --json mode emits
``{"green": bool, "checks": [{"check","pass","msg"}]}``.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.ai_talent import preflight


# -- check_replicate ----------------------------------------------------------

def test_check_replicate_fails_when_env_missing(monkeypatch):
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    ok, msg = preflight.check_replicate()
    assert ok is False
    assert "REPLICATE_API_TOKEN" in msg


def test_check_replicate_pass_when_env_set_and_auth_ok(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_fake")

    class _User:
        username = "carbon1777"

    class _Client:
        def __init__(self, *_, **__):
            self.users = SimpleNamespace(get_current=lambda: _User())

    import src.ai_talent.preflight as p

    class _Replicate:
        Client = _Client

    monkeypatch.setattr(p, "replicate", _Replicate, raising=False)
    ok, msg = p.check_replicate()
    assert ok is True
    assert "carbon1777" in msg


def test_check_replicate_fails_on_auth_exception(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "bad")

    class _Client:
        def __init__(self, *_, **__):
            raise RuntimeError("401 unauthorized")

    import src.ai_talent.preflight as p

    class _Replicate:
        Client = _Client

    monkeypatch.setattr(p, "replicate", _Replicate, raising=False)
    ok, msg = p.check_replicate()
    assert ok is False
    assert "401" in msg or "auth" in msg.lower()


# -- check_elevenlabs ---------------------------------------------------------

def test_check_elevenlabs_fails_when_env_missing(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    ok, msg = preflight.check_elevenlabs()
    assert ok is False
    assert "ELEVENLABS_API_KEY" in msg


def test_check_elevenlabs_fails_on_free_tier(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_fake")
    monkeypatch.setenv("ELEVENLABS_TIER", "free")
    ok, msg = preflight.check_elevenlabs()
    assert ok is False
    assert "tier" in msg.lower() or "free" in msg.lower()


def test_check_elevenlabs_passes_on_starter(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_fake")
    monkeypatch.setenv("ELEVENLABS_TIER", "starter")
    ok, msg = preflight.check_elevenlabs()
    assert ok is True
    assert "starter" in msg


# -- check_ltx ----------------------------------------------------------------

def test_check_ltx_pass_when_env_set(monkeypatch, tmp_path):
    monkeypatch.setenv("LTX_API_KEY", "ltxv_test123")
    monkeypatch.setattr(preflight, "DEFAULT_ENV_LTX", tmp_path / ".env.ltx")
    ok, msg = preflight.check_ltx()
    assert ok is True
    assert "env" in msg.lower()


def test_check_ltx_pass_via_env_file_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("LTX_API_KEY", raising=False)
    env_file = tmp_path / ".env.ltx"
    env_file.write_text("ltxv_fromfile456\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "DEFAULT_ENV_LTX", env_file)
    ok, msg = preflight.check_ltx()
    assert ok is True
    assert str(env_file) in msg


def test_check_ltx_fails_when_no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("LTX_API_KEY", raising=False)
    monkeypatch.setattr(preflight, "DEFAULT_ENV_LTX", tmp_path / ".env.ltx")
    ok, msg = preflight.check_ltx()
    assert ok is False
    assert "ltx" in msg.lower()


def test_check_ltx_fails_on_bad_prefix(monkeypatch, tmp_path):
    """Key without ltxv_ prefix is rejected even if env is set."""
    monkeypatch.setenv("LTX_API_KEY", "wrong_prefix_key")
    monkeypatch.setattr(preflight, "DEFAULT_ENV_LTX", tmp_path / ".env.ltx")
    ok, _ = preflight.check_ltx()
    assert ok is False


# -- check_spend_tracker ------------------------------------------------------

def test_check_spend_tracker_passes_when_no_file(tmp_path):
    """No file yet → treat as clean slate, return pass."""
    ok, msg = preflight.check_spend_tracker(tmp_path / "absent.json")
    assert ok is True


def test_check_spend_tracker_passes_on_v3_schema(tmp_path):
    f = tmp_path / "spend.json"
    f.write_text(json.dumps({
        "_schema_version": 3,
        "_updated": "2026-05-11",
        "providers": {},
    }), encoding="utf-8")
    ok, msg = preflight.check_spend_tracker(f)
    assert ok is True
    assert "v3" in msg


def test_check_spend_tracker_fails_on_v2_schema(tmp_path):
    f = tmp_path / "spend.json"
    f.write_text(json.dumps({"_schema_version": 2}), encoding="utf-8")
    ok, msg = preflight.check_spend_tracker(f)
    assert ok is False
    assert "schema" in msg.lower()


def test_check_spend_tracker_fails_on_corrupt_file(tmp_path):
    f = tmp_path / "spend.json"
    f.write_text("{ not valid json", encoding="utf-8")
    ok, msg = preflight.check_spend_tracker(f)
    assert ok is False


# -- check_character_yaml -----------------------------------------------------

def _write_yaml(p: Path, lora_status: str = "ready", voice_status: str = "ready") -> Path:
    data = {
        "lora": {"status": lora_status, "model": "x", "version_sha256": "y" * 64},
        "voice": {"status": voice_status, "voice_id": "vid"},
    }
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_check_character_yaml_fails_when_missing(tmp_path):
    ok, msg = preflight.check_character_yaml(tmp_path / "absent.yaml")
    assert ok is False
    assert "missing" in msg.lower()


def test_check_character_yaml_fails_if_lora_not_ready(tmp_path):
    f = _write_yaml(tmp_path / "char.yaml", lora_status="pending", voice_status="ready")
    ok, msg = preflight.check_character_yaml(f)
    assert ok is False
    assert "lora" in msg.lower()


def test_check_character_yaml_fails_if_voice_not_ready(tmp_path):
    f = _write_yaml(tmp_path / "char.yaml", lora_status="ready", voice_status="pending")
    ok, msg = preflight.check_character_yaml(f)
    assert ok is False
    assert "voice" in msg.lower()


def test_check_character_yaml_passes_when_both_ready(tmp_path):
    f = _write_yaml(tmp_path / "char.yaml")
    ok, msg = preflight.check_character_yaml(f)
    assert ok is True


def test_check_character_yaml_fails_on_unparseable(tmp_path):
    f = tmp_path / "char.yaml"
    f.write_text("not: a: valid: yaml: : :", encoding="utf-8")
    ok, msg = preflight.check_character_yaml(f)
    assert ok is False


# -- run_checks + main --------------------------------------------------------

def test_run_checks_returns_all_results(monkeypatch):
    """All 5 checks always run, even if one fails."""
    def _fail():
        return False, "fail"

    def _ok():
        return True, "ok"

    monkeypatch.setattr(preflight, "CHECKS", (
        ("a", _ok), ("b", _fail), ("c", _ok),
    ))
    all_pass, results = preflight.run_checks()
    assert all_pass is False
    assert len(results) == 3
    assert results[1]["pass"] is False


def test_run_checks_captures_raised_exception(monkeypatch):
    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(preflight, "CHECKS", (("z", _raise),))
    all_pass, results = preflight.run_checks()
    assert all_pass is False
    assert "boom" in results[0]["msg"]


def test_main_json_mode_emits_green_field(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "CHECKS", (("only", lambda: (True, "ok")),))
    rc = preflight.main(["--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["green"] is True
    assert payload["checks"][0]["check"] == "only"
    assert rc == 0


def test_main_exits_1_when_any_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "CHECKS", (
        ("a", lambda: (True, "ok")),
        ("b", lambda: (False, "fail")),
    ))
    rc = preflight.main([])
    out = capsys.readouterr().out
    assert "RED" in out
    assert rc == 1


def test_main_human_mode_emits_GREEN(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "CHECKS", (("a", lambda: (True, "ok")),))
    rc = preflight.main([])
    out = capsys.readouterr().out
    assert "GREEN" in out
    assert rc == 0
