"""Tests for src/tg_nudge.py — phase 1 PLAN-04 nudge templates.

All HTTP calls are mocked (monkeypatched on src.tg_nudge.requests.post).
Real network is never touched.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from src.tg_nudge import TEMPLATES, send


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def env_set(monkeypatch):
    """Provide both required env vars."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "FAKE_TOKEN_123")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "999999")


@pytest.fixture
def fake_post(monkeypatch):
    """Replace requests.post with a MagicMock returning 200 OK."""
    m = MagicMock()
    m.return_value.status_code = 200
    m.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr("src.tg_nudge.requests.post", m)
    return m


# --- Template structural tests ---------------------------------------------


def test_templates_dict_has_4_keys():
    """All 4 events must be templated (RESEARCH §«TG nudge template»)."""
    expected = {
        "monthly_plan_success",
        "monthly_plan_failure",
        "monthly_plan_brand_violation",
        "monthly_plan_budget_cap",
    }
    assert set(TEMPLATES.keys()) == expected, f"keys: {sorted(TEMPLATES.keys())}"


def test_success_template_formats():
    """success template substitutes month_ru, commit_sha7 etc."""
    out = TEMPLATES["monthly_plan_success"].format(
        month_ru="июня 2026",
        plan_path="plans/monthly_plan_2026-06.md",
        commit_url="https://github.com/Carbon1777/forton-lab-marketing/commit/abc1234",
        commit_sha7="abc1234",
        entries_count=30,
        usd_spent="0.07",
    )
    assert "июня 2026" in out
    assert "abc1234" in out
    assert "0.07" in out
    assert "30" in out
    # Must be HTML-formatted
    assert "<b>" in out or "<a" in out or "<code>" in out


def test_brand_violation_template():
    out = TEMPLATES["monthly_plan_brand_violation"].format(
        month_ru="июня 2026",
        violations_list="• «ChatGPT» в записи на 2026-06-03 (line 7)\n",
        actions_url="https://github.com/Carbon1777/forton-lab-marketing/actions",
    )
    assert "ChatGPT" in out
    assert "2026-06-03" in out
    assert "июня 2026" in out


def test_budget_cap_template():
    out = TEMPLATES["monthly_plan_budget_cap"].format(
        month_ru="июня 2026",
        usd_current="4.87",
        usd_cap="5.00",
        console_url="https://console.anthropic.com/settings/limits",
        actions_url="https://github.com/Carbon1777/forton-lab-marketing/actions",
    )
    assert "4.87" in out
    assert "5.00" in out
    assert "console.anthropic.com" in out


def test_failure_template():
    out = TEMPLATES["monthly_plan_failure"].format(
        month_ru="июня 2026",
        reason="HTTP 503 после 2 retry",
        status_url="https://status.anthropic.com",
        actions_url="https://github.com/Carbon1777/forton-lab-marketing/actions",
    )
    assert "HTTP 503" in out
    assert "status.anthropic.com" in out


# --- send() behavior --------------------------------------------------------


def test_send_calls_telegram_api(env_set, fake_post):
    rc = send(
        "monthly_plan_success",
        month_ru="июня 2026",
        plan_path="plans/x.md",
        commit_url="https://example/c/abc",
        commit_sha7="abc1234",
        entries_count=30,
        usd_spent="0.05",
    )
    assert rc == 0
    assert fake_post.call_count == 1
    # URL: positional arg or kwarg "url"
    call_args = fake_post.call_args
    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
    assert "api.telegram.org" in url
    assert "/bot" in url
    assert "/sendMessage" in url
    body = call_args.kwargs["json"]
    assert body["chat_id"] == "999999"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    assert "июня 2026" in body["text"]


def test_send_raises_on_non_200(env_set, monkeypatch):
    m = MagicMock()
    m.return_value.raise_for_status.side_effect = requests.HTTPError("500")
    monkeypatch.setattr("src.tg_nudge.requests.post", m)
    with pytest.raises(requests.HTTPError):
        send(
            "monthly_plan_success",
            month_ru="х",
            plan_path="x",
            commit_url="x",
            commit_sha7="x",
            entries_count=0,
            usd_spent="0",
        )


def test_missing_env_var_exits(monkeypatch):
    """env() helper exits 1 when env var missing — proect-style."""
    monkeypatch.delenv("TG_PLANNER_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_OWNER_CHAT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        send(
            "monthly_plan_success",
            month_ru="x",
            plan_path="x",
            commit_url="x",
            commit_sha7="x",
            entries_count=0,
            usd_spent="0",
        )
    assert exc.value.code == 1


def test_send_unknown_template_raises(env_set, fake_post):
    """KeyError on unknown template — explicit, not silent."""
    with pytest.raises(KeyError):
        send("not_a_template", month_ru="x")


# --- CLI entry --------------------------------------------------------------


def test_cli_entry_no_args_returns_1(monkeypatch, capsys):
    from src.tg_nudge import _main_from_argv

    rc = _main_from_argv(["tg_nudge"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_cli_entry_reads_nudge_env_vars(env_set, fake_post, monkeypatch):
    from src.tg_nudge import _main_from_argv

    # Set NUDGE_<KEY> env vars — they should be lower-cased and stripped of prefix
    monkeypatch.setenv("NUDGE_MONTH_RU", "июня 2026")
    monkeypatch.setenv("NUDGE_PLAN_PATH", "plans/x.md")
    monkeypatch.setenv("NUDGE_COMMIT_URL", "https://example/c/abc")
    monkeypatch.setenv("NUDGE_COMMIT_SHA7", "abc1234")
    monkeypatch.setenv("NUDGE_ENTRIES_COUNT", "30")
    monkeypatch.setenv("NUDGE_USD_SPENT", "0.07")

    rc = _main_from_argv(["tg_nudge", "monthly_plan_success"])
    assert rc == 0
    body = fake_post.call_args.kwargs["json"]
    assert "июня 2026" in body["text"]
    assert "abc1234" in body["text"]
