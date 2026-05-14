"""Unit tests for src/store_metrics/hypothesis.py — Claude Haiku 4.5 (METRICS-09).

All Anthropic SDK calls are mocked via ``mock_anthropic_haiku`` fixture (conftest)
+ patching of ``anthropic.Anthropic`` constructor and spend_tracker_v2 helpers.

Fixtures consumed:
    haiku_hypothesis_response_clean.json     — 3 brand-clean insights
    haiku_hypothesis_response_brand_violation.json — 1 clean + 2 violators
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import hypothesis
from src.store_metrics.models import (
    ProductReport,
    StoreSnapshot,
    TrendPoint,
    WeeklyReport,
)
from src.spend_tracker_v2 import (
    DailyCapExceededError,
    MonthlyAbortError,
    ProviderMonthlyCapExceededError,
)


# ===================================================================
# Test helpers
# ===================================================================

def _make_report() -> WeeklyReport:
    """Build a minimal WeeklyReport mirroring _make_report() in test_digest."""
    week = dt.date(2026, 5, 5)
    prev_week = week - dt.timedelta(days=7)

    def _snap(product, store, installs, rating=4.5, week_start=week, error=None):
        return StoreSnapshot(
            product=product, store=store, week_start=week_start,
            installs=installs, rating=rating,
            top_country="RU", top_country_share=0.78, error=error,
        )

    centry = ProductReport(
        product="centry",
        snapshots=[
            _snap("centry", "app_store", 30, 4.7),
            _snap("centry", "google_play", 15, 4.6),
            _snap("centry", "rustore", 5, 4.8),
        ],
        prev_snapshots=[
            _snap("centry", "app_store", 20, 4.7, week_start=prev_week),
            _snap("centry", "google_play", 22, 4.6, week_start=prev_week),
            _snap("centry", "rustore", 3, 4.8, week_start=prev_week),
        ],
        trend_4w=[
            TrendPoint(week_start=week - dt.timedelta(days=21), installs=35),
            TrendPoint(week_start=week - dt.timedelta(days=14), installs=40),
            TrendPoint(week_start=week - dt.timedelta(days=7), installs=45),
            TrendPoint(week_start=week, installs=50),
        ],
    )
    diktum = ProductReport(
        product="diktum",
        snapshots=[
            _snap("diktum", "app_store", 18, 4.6),
            _snap("diktum", "google_play", 9, 4.5),
            _snap("diktum", "rustore", 2, 4.7),
        ],
        prev_snapshots=[
            _snap("diktum", "app_store", 22, 4.6, week_start=prev_week),
            _snap("diktum", "google_play", 8, 4.5, week_start=prev_week),
            _snap("diktum", "rustore", 2, 4.7, week_start=prev_week),
        ],
        trend_4w=[],
    )
    return WeeklyReport(week_start=week, products=[centry, diktum])


def _make_response_msg(text: str, in_tokens: int = 300, out_tokens: int = 80):
    """Build a mock Anthropic Message response carrying text."""
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=text)]
    msg.stop_reason = "end_turn"
    msg.usage = MagicMock(input_tokens=in_tokens, output_tokens=out_tokens)
    return msg


@pytest.fixture
def tmp_spend(tmp_path) -> Path:
    """Empty spend tracker for hypothesis tests (no caps blocked)."""
    p = tmp_path / "api_spend.json"
    p.write_text(json.dumps({"_schema_version": 3, "_updated": None}), encoding="utf-8")
    return p


# ===================================================================
# _is_configured()
# ===================================================================

def test_is_configured_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    assert hypothesis._is_configured() is True


def test_is_configured_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert hypothesis._is_configured() is False


def test_is_configured_empty_string(monkeypatch):
    """Empty string treated as absent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert hypothesis._is_configured() is False


# ===================================================================
# _build_prompt()
# ===================================================================

def test_build_prompt_returns_system_and_user():
    report = _make_report()
    system, user = hypothesis._build_prompt(report)
    assert isinstance(system, str) and isinstance(user, str)
    assert len(system) > 100
    assert len(user) > 50


def test_build_prompt_system_contains_format_instruction():
    report = _make_report()
    system, _ = hypothesis._build_prompt(report)
    assert "JSON" in system
    assert "insights" in system
    assert "≤90" in system   # length constraint
    assert "Forton Lab" in system


def test_build_prompt_includes_product_data():
    """User message must carry product installs + WoW deltas."""
    report = _make_report()
    _, user = hypothesis._build_prompt(report)
    # Centry installs: 30 (app_store), 15 (google_play), 5 (rustore)
    assert '"product": "centry"' in user
    assert '"product": "diktum"' in user
    assert "app_store" in user
    assert "google_play" in user
    assert "rustore" in user
    assert '"installs"' in user
    assert '"wow_pct"' in user


def test_build_prompt_includes_trend_data():
    """4-week trend present in serialized JSON."""
    report = _make_report()
    _, user = hypothesis._build_prompt(report)
    assert "trend_4w" in user
    # Centry trend points: installs 35/40/45/50
    assert "35" in user
    assert "45" in user


def test_build_prompt_computes_wow_pct():
    """Verify Centry GP WoW: 15 vs prev 22 → (15-22)/22*100 = -31.8."""
    report = _make_report()
    _, user = hypothesis._build_prompt(report)
    # Parse the embedded JSON to verify wow_pct math
    # User msg has JSON pretty-printed in middle. Pull it out by regex of leading {
    import re
    m = re.search(r"\{[^{]*\"products\".*\}\s*\n", user, re.DOTALL)
    assert m is not None, "expected embedded JSON in user prompt"
    data = json.loads(m.group(0))
    centry = next(p for p in data["products"] if p["product"] == "centry")
    gp_store = next(s for s in centry["stores"] if s["store"] == "google_play")
    assert gp_store["wow_pct"] == pytest.approx(-31.8, abs=0.1)


def test_build_prompt_handles_zero_prev():
    """Avoid ZeroDivisionError when prev_installs=0."""
    week = dt.date(2026, 5, 5)
    snap = StoreSnapshot(product="centry", store="app_store",
                         week_start=week, installs=10)
    prev_snap = StoreSnapshot(product="centry", store="app_store",
                              week_start=week - dt.timedelta(days=7), installs=0)
    prod = ProductReport(
        product="centry", snapshots=[snap], prev_snapshots=[prev_snap], trend_4w=[],
    )
    report = WeeklyReport(week_start=week, products=[prod])
    _, user = hypothesis._build_prompt(report)
    # Should not raise; wow_pct should be null (None)
    assert "wow_pct" in user


# ===================================================================
# _strip_code_fence()
# ===================================================================

def test_strip_code_fence_plain_json():
    txt = '{"insights": ["a"]}'
    assert hypothesis._strip_code_fence(txt) == '{"insights": ["a"]}'


def test_strip_code_fence_with_json_tag():
    txt = '```json\n{"insights": ["a"]}\n```'
    assert hypothesis._strip_code_fence(txt) == '{"insights": ["a"]}'


def test_strip_code_fence_without_tag():
    txt = '```\n{"insights": ["a"]}\n```'
    assert hypothesis._strip_code_fence(txt) == '{"insights": ["a"]}'


def test_strip_code_fence_with_whitespace():
    txt = '   \n```json\n{"insights": ["a"]}\n```\n  '
    assert hypothesis._strip_code_fence(txt) == '{"insights": ["a"]}'


# ===================================================================
# _parse_insights()
# ===================================================================

def test_parse_insights_valid_json():
    text = '{"insights": ["first", "second"]}'
    assert hypothesis._parse_insights(text) == ["first", "second"]


def test_parse_insights_truncates_long_strings():
    long = "x" * 200
    text = json.dumps({"insights": [long]})
    out = hypothesis._parse_insights(text)
    assert len(out) == 1
    assert len(out[0]) == hypothesis.MAX_INSIGHT_CHARS  # 90


def test_parse_insights_caps_at_3():
    """Even if model returns 5 insights, only first 3 kept."""
    text = json.dumps({"insights": ["a", "b", "c", "d", "e"]})
    out = hypothesis._parse_insights(text)
    assert out == ["a", "b", "c"]


def test_parse_insights_drops_empty_strings():
    text = json.dumps({"insights": ["valid", "", "  ", "also valid"]})
    out = hypothesis._parse_insights(text)
    assert out == ["valid", "also valid"]


def test_parse_insights_drops_non_strings():
    text = json.dumps({"insights": ["good", 42, None, ["nested"], "alsogood"]})
    out = hypothesis._parse_insights(text)
    assert out == ["good", "alsogood"]


def test_parse_insights_handles_markdown_codeblock_wrap():
    """Pitfall 6 — Haiku sometimes wraps JSON in ```json fences."""
    text = '```json\n{"insights": ["a", "b"]}\n```'
    out = hypothesis._parse_insights(text)
    assert out == ["a", "b"]


def test_parse_insights_invalid_json_returns_empty():
    out = hypothesis._parse_insights("not valid json at all { )(")
    assert out == []


def test_parse_insights_missing_insights_key_returns_empty():
    text = json.dumps({"other_key": ["a"]})
    out = hypothesis._parse_insights(text)
    assert out == []


def test_parse_insights_not_a_dict_returns_empty():
    text = json.dumps(["just", "a", "list"])
    out = hypothesis._parse_insights(text)
    assert out == []


def test_parse_insights_insights_not_a_list_returns_empty():
    text = json.dumps({"insights": "single string"})
    out = hypothesis._parse_insights(text)
    assert out == []


def test_parse_insights_strips_whitespace():
    text = json.dumps({"insights": ["  hello  ", "\nworld\n"]})
    out = hypothesis._parse_insights(text)
    assert out == ["hello", "world"]


# ===================================================================
# _filter_brand_violations()
# ===================================================================

def test_filter_brand_violations_drops_offending_insight():
    """`Claude` is in brand_lint stop list — should be dropped."""
    insights = [
        "Centry стабильно растёт",
        "Diktum работает на Claude в проде",  # stop word: claude
    ]
    out = hypothesis._filter_brand_violations(insights)
    assert out == ["Centry стабильно растёт"]


def test_filter_brand_violations_keeps_all_clean():
    insights = [
        "Centry установок растёт стабильно",
        "Diktum просел — что-то с конверсией?",
        "RuStore на минимуме",
    ]
    out = hypothesis._filter_brand_violations(insights)
    assert out == insights


def test_filter_brand_violations_drops_all_returns_empty():
    insights = [
        "Алексей придумал фичу",       # name stop
        "Flutter и Supabase основа",   # stack stop ×2
    ]
    out = hypothesis._filter_brand_violations(insights)
    assert out == []


def test_filter_brand_violations_drops_marketing_fluff():
    """Substring match on marketing roots — «инновацион»."""
    insights = [
        "Centry — наш инновационный продукт",   # marketing root
        "Diktum просел в GP",                   # clean
    ]
    out = hypothesis._filter_brand_violations(insights)
    assert out == ["Diktum просел в GP"]


# ===================================================================
# generate() — full integration
# ===================================================================

def test_generate_unconfigured_returns_empty(monkeypatch, tmp_spend):
    """No API key → empty list, no API call attempted."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = hypothesis.generate(_make_report(), spend_file=tmp_spend)
    assert out == []


def test_generate_clean_response_returns_insights(monkeypatch, tmp_spend,
                                                    fixture_store_metrics_dir):
    """Mock Haiku returns 3 clean insights → all 3 surface unchanged."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    fixture_text = (fixture_store_metrics_dir / "haiku_hypothesis_response_clean.json").read_text()

    msg = _make_response_msg(fixture_text, in_tokens=1200, out_tokens=180)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = msg

    with patch("anthropic.Anthropic", return_value=fake_client) as mock_ctor:
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    mock_ctor.assert_called_once()
    assert len(out) == 3
    assert all(isinstance(s, str) for s in out)
    assert "Centry" in out[0]
    # Spend file should contain anthropic entry now
    saved = json.loads(tmp_spend.read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    assert "anthropic" in saved[month_key]["by_provider"]
    assert saved[month_key]["by_provider"]["anthropic"]["calls"] == 1


def test_generate_brand_violation_response_filtered(monkeypatch, tmp_spend,
                                                      fixture_store_metrics_dir):
    """Fixture has 1 clean + 2 violators → only the clean one surfaces."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    fixture_text = (
        fixture_store_metrics_dir / "haiku_hypothesis_response_brand_violation.json"
    ).read_text()

    msg = _make_response_msg(fixture_text)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = msg

    with patch("anthropic.Anthropic", return_value=fake_client):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    # Only "Centry показывает стабильный рост..." passes — others contain
    # Алексей / Flutter / Supabase (stop words).
    assert len(out) == 1
    assert "Centry" in out[0]


def test_generate_records_spend_on_success(monkeypatch, tmp_spend):
    """Verify record_provider_spend writes to disk with provider=anthropic."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    msg = _make_response_msg(
        '{"insights":["test ok"]}', in_tokens=1500, out_tokens=200,
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = msg

    with patch("anthropic.Anthropic", return_value=fake_client):
        hypothesis.generate(_make_report(), spend_file=tmp_spend)

    saved = json.loads(tmp_spend.read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    anth = saved[month_key]["by_provider"]["anthropic"]
    # Expected USD: 1500/1M * 1 + 200/1M * 5 = 0.0015 + 0.001 = 0.0025
    assert anth["usd"] == pytest.approx(0.0025, rel=0.01)
    assert anth["calls"] == 1
    assert anth["output_tokens"] == 200


def test_generate_preflight_daily_cap_returns_empty(monkeypatch, tmp_spend):
    """preflight_check raises DailyCapExceededError → return [] gracefully."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    with patch(
        "src.store_metrics.hypothesis.preflight_check",
        side_effect=DailyCapExceededError("daily $4.0+$0.005>$3.0"),
    ):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    assert out == []


def test_generate_preflight_monthly_abort_returns_empty(monkeypatch, tmp_spend):
    """preflight_check raises MonthlyAbortError → return [] gracefully."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    with patch(
        "src.store_metrics.hypothesis.preflight_check",
        side_effect=MonthlyAbortError("monthly hit $15"),
    ):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    assert out == []


def test_generate_preflight_provider_cap_returns_empty(monkeypatch, tmp_spend):
    """preflight_check raises ProviderMonthlyCapExceededError → []."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    with patch(
        "src.store_metrics.hypothesis.preflight_check",
        side_effect=ProviderMonthlyCapExceededError("anthropic cap"),
    ):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    assert out == []


def test_generate_api_error_returns_empty(monkeypatch, tmp_spend):
    """Anthropic SDK raises APIError → soft-fallback to []."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    # Trigger error inside _call_haiku_raw by making the client raise
    from anthropic import APIError
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = APIError(
        "boom", request=MagicMock(), body=None,
    )

    with patch("anthropic.Anthropic", return_value=fake_client):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    assert out == []


def test_generate_never_raises_on_unexpected_error(monkeypatch, tmp_spend):
    """Even an unexpected ValueError mid-call → return [] (hard guarantee)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = ValueError("unexpected boom")

    with patch("anthropic.Anthropic", return_value=fake_client):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    assert out == []


def test_generate_malformed_json_response_returns_empty(monkeypatch, tmp_spend):
    """Haiku returns non-JSON gibberish → parse fails → []."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    msg = _make_response_msg("not even json {[(")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = msg

    with patch("anthropic.Anthropic", return_value=fake_client):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    # malformed JSON → empty list (insight parsing fails); but spend WAS
    # already recorded for the call that happened.
    assert out == []
    saved = json.loads(tmp_spend.read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    # spend recorded even on parse failure (we paid for the call)
    assert saved[month_key]["by_provider"]["anthropic"]["calls"] == 1


def test_generate_default_spend_file_when_none(monkeypatch):
    """If spend_file=None, default Path('.metrics/api_spend.json') is used."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No key → early return before touching disk
    out = hypothesis.generate(_make_report(), spend_file=None)
    assert out == []


def test_generate_passes_default_spend_path_to_call(monkeypatch, tmp_path):
    """When spend_file=None and key set, default path is forwarded to _call_haiku_raw."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.chdir(tmp_path)

    with patch(
        "src.store_metrics.hypothesis._call_haiku_raw",
        return_value='{"insights":["ok"]}',
    ) as mock_call:
        hypothesis.generate(_make_report())  # spend_file=None → default

    # _call_haiku_raw(system, user, spend_file) — 3rd positional is the path
    args, kwargs = mock_call.call_args
    assert args[2] == Path(".metrics/api_spend.json")


def test_generate_all_violations_returns_empty(monkeypatch, tmp_spend):
    """Haiku returns 3 insights all with brand violations → digest gets []."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    payload = json.dumps({
        "insights": [
            "Алексей запустил Centry",      # name
            "Diktum на Flutter работает",   # stack
            "Уникальный продукт студии",    # marketing root
        ],
    })
    msg = _make_response_msg(payload)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = msg

    with patch("anthropic.Anthropic", return_value=fake_client):
        out = hypothesis.generate(_make_report(), spend_file=tmp_spend)

    assert out == []


# ===================================================================
# WeeklyReport.hypotheses field (additive extension verification)
# ===================================================================

def test_weekly_report_hypotheses_defaults_to_empty():
    """Additive field — existing call-sites without hypotheses= keep working."""
    week = dt.date(2026, 5, 5)
    report = WeeklyReport(week_start=week, products=[])
    assert report.hypotheses == []


def test_weekly_report_hypotheses_accepts_list():
    """Field accepts list of insight strings."""
    week = dt.date(2026, 5, 5)
    insights = ["Centry стабильно", "Diktum просел"]
    report = WeeklyReport(
        week_start=week, products=[], hypotheses=insights,
    )
    assert report.hypotheses == insights
