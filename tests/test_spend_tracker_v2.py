"""Unit tests for spend_tracker_v2 helpers (Phase 1.5 Wave 0)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.spend_tracker_v2 import (
    DEFAULT_REGEN_LIMIT,
    increment_regen_count,
    read_regen_count,
    read_regen_limit,
)


def test_default_regen_limit_is_three():
    """D-1.5-03 invariant: 3 regenerate/month."""
    assert DEFAULT_REGEN_LIMIT == 3


def test_read_regen_count_v1_compat(tmp_path):
    """v1 schema (no regen_count) reads as 0 — backward-compat invariant."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({
        "_schema_version": 1,
        "_updated": "2026-05-01T00:00:00Z",
        "2026-05": {"input_tokens": 100, "output_tokens": 50, "usd": 0.001, "calls": 1},
    }))
    assert read_regen_count(f, "2026-05") == 0


def test_read_regen_limit_v1_compat(tmp_path):
    """v1 schema (no regen_limit_per_month) → default."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({"_schema_version": 1}))
    assert read_regen_limit(f) == DEFAULT_REGEN_LIMIT
    assert read_regen_limit(f, default=5) == 5


def test_read_regen_count_missing_file(tmp_path):
    """Absent file → 0 (no crash, T-1.5-02 mitigation)."""
    assert read_regen_count(tmp_path / "nope.json", "2026-06") == 0


def test_read_regen_limit_missing_file(tmp_path):
    """Absent file → DEFAULT_REGEN_LIMIT."""
    assert read_regen_limit(tmp_path / "nope.json") == DEFAULT_REGEN_LIMIT


def test_read_regen_count_corrupt_json(tmp_path, capsys):
    """Corrupt JSON → 0 with stderr warning."""
    f = tmp_path / "broken.json"
    f.write_text("{not valid json")
    assert read_regen_count(f, "2026-06") == 0
    captured = capsys.readouterr()
    assert "unreadable" in captured.err


def test_read_regen_count_v2_returns_int(tmp_path):
    """v2 schema with regen_count returns int."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({
        "_schema_version": 2,
        "2026-06": {
            "regen_count": 2,
            "input_tokens": 0,
            "output_tokens": 0,
            "usd": 0.18,
            "calls": 2,
        },
        "regen_limit_per_month": 3,
    }))
    assert read_regen_count(f, "2026-06") == 2
    assert read_regen_limit(f) == 3


def test_read_regen_count_other_month_zero(tmp_path):
    """Reading a month not in the file returns 0."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({
        "_schema_version": 2,
        "2026-06": {"regen_count": 2},
    }))
    assert read_regen_count(f, "2026-07") == 0


def test_increment_regen_count_creates_v2_schema(tmp_path):
    """Increment on v1 file bumps schema to 2 and writes regen_count."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({"_schema_version": 1, "2026-06": {"calls": 1}}))
    new_count = increment_regen_count(f, "2026-06")
    assert new_count == 1
    data = json.loads(f.read_text())
    assert data["_schema_version"] == 2
    assert data["2026-06"]["regen_count"] == 1
    assert data["2026-06"]["calls"] == 1  # preserved
    assert data["regen_limit_per_month"] == DEFAULT_REGEN_LIMIT
    assert "_updated" in data


def test_increment_regen_count_idempotent_count(tmp_path):
    """Two increments → count == 2."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({"_schema_version": 2, "regen_limit_per_month": 3}))
    assert increment_regen_count(f, "2026-06") == 1
    assert increment_regen_count(f, "2026-06") == 2
    assert read_regen_count(f, "2026-06") == 2


def test_increment_regen_count_atomic_write(tmp_path):
    """File still parseable after increment (tmp+replace, no half-write)."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({"_schema_version": 1}))
    increment_regen_count(f, "2026-06")
    # If atomic write failed, this would raise JSONDecodeError
    data = json.loads(f.read_text())
    assert data["_schema_version"] == 2
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_increment_regen_count_creates_dirs(tmp_path):
    """Increment creates parent dir tree if absent (e.g. fresh checkout)."""
    f = tmp_path / "deep" / "nested" / "api_spend.json"
    new_count = increment_regen_count(f, "2026-06")
    assert new_count == 1
    assert f.exists()


def test_increment_regen_count_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """If os.replace raises, the temp file is unlinked and exception re-raised."""
    import src.spend_tracker_v2 as mod

    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({"_schema_version": 1}))

    def boom(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        increment_regen_count(f, "2026-06")
    # No leftover .tmp files — except handler unlinks them
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    # Original file untouched (atomicity invariant)
    assert json.loads(f.read_text()) == {"_schema_version": 1}
