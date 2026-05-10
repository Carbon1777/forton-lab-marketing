"""Unit tests for spend_tracker_v2 helpers (Phase 1.5 Wave 0)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.spend_tracker_v2 import (
    DEFAULT_DAILY_CAP_USD,
    DEFAULT_MONTHLY_ABORT_USD,
    DEFAULT_PROVIDER_MONTHLY_CAPS,
    DEFAULT_REGEN_LIMIT,
    DailyCapExceededError,
    MonthlyAbortError,
    PROVIDER_UNIT_FIELDS,
    ProviderMonthlyCapExceededError,
    increment_regen_count,
    preflight_check,
    read_daily_spend,
    read_monthly_total,
    read_provider_spend,
    read_regen_count,
    read_regen_limit,
    record_provider_spend,
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


# ============================================================================
# Phase 7 (BOOT-01 / BOOT-05) — v3 multi-provider spend tracking
# Contract: 07-SCHEMA-v3.md
# ============================================================================

import datetime as dt


def _today_month():
    today = dt.date.today().isoformat()
    return today, today[:7]


def test_default_caps_constants_are_locked():
    """BOOT-01: default cap constants match 07-SCHEMA-v3 §3 — single source of truth.

    Open issue from 07-01-SUMMARY §2: runtime MUST read from constants, NOT from
    JSON `caps` block. This test pins the constants so callers can rely on them.
    """
    assert DEFAULT_DAILY_CAP_USD == 3.0
    assert DEFAULT_MONTHLY_ABORT_USD == 15.0
    assert DEFAULT_PROVIDER_MONTHLY_CAPS == {
        "anthropic": 5.0,
        "replicate": 4.0,
        "elevenlabs": 5.0,
        "ltx": 6.0,
    }
    assert PROVIDER_UNIT_FIELDS == {
        "anthropic": None,
        "replicate": "predict_seconds",
        "elevenlabs": "characters",
        "ltx": "seconds",
    }


def test_record_provider_spend_creates_v3_fields(tmp_path):
    """BOOT-01: record_provider_spend bumps schema to v3 and adds by_provider/by_day."""
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "replicate", 0.5)
    data = json.loads(f.read_text(encoding="utf-8"))
    today, month = _today_month()
    assert data["_schema_version"] == 3
    assert "_updated" in data
    assert data[month]["by_provider"]["replicate"]["usd"] == 0.5
    assert data[month]["by_provider"]["replicate"]["calls"] == 1
    assert data[month]["by_day"][today]["usd"] == 0.5
    assert data[month]["by_day"][today]["by_provider"]["replicate"] == 0.5


def test_record_provider_spend_v2_reader_still_works(tmp_path):
    """BOOT-01 backward-compat: v2 readers function on v3-shaped file."""
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "anthropic", 0.1)
    _, month = _today_month()
    # v2 readers must not raise and must return v2-shaped defaults
    assert read_regen_count(f, month) == 0
    assert read_regen_limit(f) == DEFAULT_REGEN_LIMIT


def test_record_provider_spend_with_units_field(tmp_path):
    """BOOT-01: explicit unit_field='characters' + units=1500 writes to by_provider."""
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "elevenlabs", 0.3, units=1500, unit_field="characters")
    _, month = _today_month()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data[month]["by_provider"]["elevenlabs"]["characters"] == 1500


def test_record_provider_spend_uses_default_unit_field_per_provider(tmp_path):
    """BOOT-01: unit_field=None → fall back to PROVIDER_UNIT_FIELDS (e.g. ltx→seconds)."""
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "ltx", 0.6, units=12)
    _, month = _today_month()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data[month]["by_provider"]["ltx"]["seconds"] == 12


def test_record_provider_spend_anthropic_skips_units_when_no_default_field(tmp_path):
    """BOOT-01: anthropic has unit_field=None — units arg ignored without explicit field.

    Anthropic tokens live in top-level v2 fields (input_tokens/output_tokens), not
    in by_provider — this preserves the v2 contract.
    """
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "anthropic", 0.05, units=1000)
    _, month = _today_month()
    data = json.loads(f.read_text(encoding="utf-8"))
    pe = data[month]["by_provider"]["anthropic"]
    # usd/calls present, but no unit key written
    assert pe["usd"] == 0.05
    assert pe["calls"] == 1
    assert "characters" not in pe and "predict_seconds" not in pe and "seconds" not in pe


def test_record_provider_spend_accumulates_across_calls(tmp_path):
    """BOOT-01: two calls → sums correct in both by_provider and by_day."""
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "replicate", 0.5, units=10, unit_field="predict_seconds")
    record_provider_spend(f, "replicate", 0.3, units=4, unit_field="predict_seconds")
    today, month = _today_month()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data[month]["by_provider"]["replicate"]["usd"] == 0.8
    assert data[month]["by_provider"]["replicate"]["calls"] == 2
    assert data[month]["by_provider"]["replicate"]["predict_seconds"] == 14
    assert data[month]["by_day"][today]["usd"] == 0.8
    assert data[month]["by_day"][today]["by_provider"]["replicate"] == 0.8


def test_record_provider_spend_creates_directory_if_missing(tmp_path):
    """BOOT-01: parent dir auto-created (analog v2 test_increment_regen_count_creates_dirs)."""
    f = tmp_path / "deep" / "nested" / "api_spend.json"
    record_provider_spend(f, "ltx", 0.1)
    assert f.exists()


def test_read_provider_spend_returns_zero_for_missing(tmp_path):
    """BOOT-01: read_provider_spend on empty/absent file → 0.0, no KeyError."""
    f = tmp_path / "api_spend.json"
    assert read_provider_spend(f, "2026-05", "replicate") == 0.0
    # File exists but month missing
    f.write_text(json.dumps({"_schema_version": 3}))
    assert read_provider_spend(f, "2026-05", "replicate") == 0.0


def test_read_daily_spend_returns_zero_for_missing_day(tmp_path):
    """BOOT-01: read_daily_spend on day absent from by_day → 0.0."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({
        "_schema_version": 3,
        "2026-05": {"by_day": {"2026-05-10": {"usd": 0.5, "by_provider": {}}}},
    }))
    assert read_daily_spend(f, "2026-05-11") == 0.0
    assert read_daily_spend(f, "2026-05-10") == 0.5


def test_read_monthly_total_v2_shape_fallback(tmp_path):
    """BOOT-01: file with v2 top-level usd (no by_provider) → returns top-level usd."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({
        "_schema_version": 2,
        "2026-05": {"usd": 0.2765, "calls": 3},
    }))
    assert read_monthly_total(f, "2026-05") == 0.2765


def test_read_monthly_total_v3_shape_sums_providers(tmp_path):
    """BOOT-01: file with by_provider → sum across providers."""
    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({
        "_schema_version": 3,
        "2026-05": {
            "by_provider": {
                "anthropic": {"usd": 0.1, "calls": 1},
                "replicate": {"usd": 0.5, "calls": 2},
                "ltx": {"usd": 0.4, "calls": 1},
            },
        },
    }))
    assert read_monthly_total(f, "2026-05") == 1.0


def test_preflight_check_under_caps_passes(tmp_path):
    """BOOT-01: empty file, small est → no raise."""
    f = tmp_path / "api_spend.json"
    preflight_check(f, "anthropic", 0.5)  # under all caps


def test_preflight_check_daily_cap_raises(tmp_path):
    """BOOT-01: $3/day hard cap — record $2.9 then preflight $0.5 → DailyCapExceededError.

    Uses anthropic provider with a high provider_monthly_cap override so the
    provider layer doesn't fire first (provider cap is checked before daily).
    """
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "anthropic", 2.9)
    with pytest.raises(DailyCapExceededError):
        # Override provider cap so we deliberately hit daily layer
        preflight_check(f, "anthropic", 0.5, provider_monthly_cap=100.0)


def test_preflight_check_monthly_abort_raises(tmp_path):
    """BOOT-01: $15/month hard abort — record $14.8, preflight $0.5 → MonthlyAbortError.

    Override daily_cap and provider_monthly_cap to high values so the monthly
    abort layer is the one that fires.
    """
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "anthropic", 14.8)
    with pytest.raises(MonthlyAbortError):
        preflight_check(
            f, "anthropic", 0.5,
            daily_cap=100.0, provider_monthly_cap=100.0,
        )


def test_preflight_check_provider_cap_raises(tmp_path):
    """BOOT-05: provider monthly cap — replicate $3.7 + $0.5 > $4.0 → ProviderMonthlyCapExceededError."""
    f = tmp_path / "api_spend.json"
    record_provider_spend(f, "replicate", 3.7, units=70, unit_field="predict_seconds")
    with pytest.raises(ProviderMonthlyCapExceededError):
        preflight_check(f, "replicate", 0.5)


def test_preflight_check_provider_cap_fires_before_daily(tmp_path):
    """BOOT-01: failure order — provider cap (narrowest) checked first.

    Construct a state where BOTH provider cap AND daily cap would trip;
    assert the provider error wins.
    """
    f = tmp_path / "api_spend.json"
    # Replicate already at $3.7 (cap $4.0) AND today's total is $3.7 (daily cap $3.0).
    record_provider_spend(f, "replicate", 3.7, units=70, unit_field="predict_seconds")
    # $0.5 more would trip both: provider 3.7+0.5=4.2>4.0 AND daily 3.7+0.5=4.2>3.0.
    with pytest.raises(ProviderMonthlyCapExceededError):
        preflight_check(f, "replicate", 0.5)


def test_record_provider_spend_atomic_on_replace_failure(tmp_path, monkeypatch):
    """BOOT-01: os.replace failure → tmp file unlinked, original preserved."""
    import src.spend_tracker_v2 as mod

    f = tmp_path / "api_spend.json"
    f.write_text(json.dumps({"_schema_version": 2, "regen_limit_per_month": 3}))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        record_provider_spend(f, "replicate", 0.5)
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    # Original file untouched (atomicity invariant)
    data = json.loads(f.read_text())
    assert data == {"_schema_version": 2, "regen_limit_per_month": 3}


def test_record_provider_spend_preserves_v2_fields(tmp_path):
    """BOOT-01 backward-compat: v3 writer MUST NOT touch v1/v2 fields.

    07-SCHEMA-v3 §3 explicitly forbids record_provider_spend from modifying
    top-level monthly usd / calls / input_tokens / output_tokens / by_purpose
    / regen_count. Those remain owned by monthly_plan_generator.record_spend
    and increment_regen_count.
    """
    f = tmp_path / "api_spend.json"
    # Seed a v2 file with all the protected fields populated
    _, month = _today_month()
    seed = {
        "_schema_version": 2,
        "_updated": "2026-05-01T00:00:00Z",
        month: {
            "input_tokens": 17031,
            "output_tokens": 15025,
            "usd": 0.2765,
            "calls": 3,
            "by_purpose": {"monthly_plan": {"calls": 3, "usd": 0.2765}},
            "regen_count": 2,
        },
        "regen_limit_per_month": 3,
    }
    f.write_text(json.dumps(seed))

    record_provider_spend(f, "replicate", 0.5)

    data = json.loads(f.read_text())
    # Protected v1/v2 fields preserved verbatim
    assert data[month]["input_tokens"] == 17031
    assert data[month]["output_tokens"] == 15025
    assert data[month]["usd"] == 0.2765
    assert data[month]["calls"] == 3
    assert data[month]["by_purpose"] == {"monthly_plan": {"calls": 3, "usd": 0.2765}}
    assert data[month]["regen_count"] == 2
    assert data["regen_limit_per_month"] == 3
    # v3 additions present
    assert data["_schema_version"] == 3
    assert data[month]["by_provider"]["replicate"]["usd"] == 0.5
