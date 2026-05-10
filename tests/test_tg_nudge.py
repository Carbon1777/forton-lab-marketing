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


# ============================================================
# Phase 1.5 Plan 03 — send_weekly_split (APPROVE-01)
# ============================================================

import datetime as _dt_15p3
import dataclasses as _dc_15p3
from unittest.mock import MagicMock as _Mock15p3


def _make_plan(num_days: int = 30, month: str = "2026-06"):
    """Build a synthetic Plan with num_days entries, slugs/channels/excerpt set."""
    from src.plan_reader import Plan, PlanEntry, Media
    year, m = map(int, month.split("-"))
    entries = []
    for i in range(1, num_days + 1):
        d = _dt_15p3.date(year, m, i)
        media = [Media(path=f"assets/img_{i}.png", sha256="a" * 64)] if i % 3 == 0 else []
        entries.append(PlanEntry(
            date=d,
            slug=f"forton-{month.replace('-', '')}-{i:02d}",
            channels=["tg", "vk"] if i % 2 == 0 else ["tg"],
            product="forton-lab",
            rubric="from_studio",
            media=media,
            status="draft",
            content=f"Это контент для дня {i}. Текст про студию Forton Lab и наши приложения. " * 3,
        ))
    return Plan(
        month=month,
        entries=entries,
        generated_at=_dt_15p3.datetime(year, m, 1, 7, 23, 14, tzinfo=_dt_15p3.timezone.utc),
    )


def _stub_tg_response(message_id: int):
    r = _Mock15p3()
    r.status_code = 200
    r.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    r.raise_for_status = _Mock15p3()
    return r


def test_weekly_split_keyboard_only_last(mocker, monkeypatch):
    """reply_markup only in the LAST sendMessage call; absent in earlier ones."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    kb = [[{"text": "ok", "callback_data": "approve:abc12345"}]]

    # 5 ISO-weeks in June 2026 → 5 sendMessage calls; stub responses 101..105
    responses = [_stub_tg_response(100 + i) for i in range(1, 6)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")  # no real sleep in tests

    ids = send_weekly_split(plan, inline_keyboard=kb, pause_between_s=0.2)

    # Exactly 5 calls (ISO-weeks 23/24/25/26/27 in June 2026)
    assert spy.call_count == 5
    # First 4 calls — no reply_markup
    for i in range(4):
        body = spy.call_args_list[i].kwargs["json"]
        assert "reply_markup" not in body
    # Last call — has reply_markup
    last_body = spy.call_args_list[4].kwargs["json"]
    assert last_body["reply_markup"] == {"inline_keyboard": kb}
    assert ids == [101, 102, 103, 104, 105]


def test_weekly_split_silent_except_last(mocker, monkeypatch):
    """disable_notification=True for all but the LAST message."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    responses = [_stub_tg_response(100 + i) for i in range(1, 6)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    send_weekly_split(plan, inline_keyboard=[[{"text": "x", "callback_data": "approve:00000000"}]])

    for i in range(4):
        assert spy.call_args_list[i].kwargs["json"]["disable_notification"] is True
    assert spy.call_args_list[4].kwargs["json"]["disable_notification"] is False


def test_weekly_message_under_limit(mocker, monkeypatch):
    """Each weekly message ≤ 4096 chars (TG limit)."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    responses = [_stub_tg_response(i) for i in range(101, 106)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    send_weekly_split(plan, inline_keyboard=None)

    for c in spy.call_args_list:
        text = c.kwargs["json"]["text"]
        assert len(text) <= 4096, f"weekly message exceeds 4096 chars: {len(text)}"


def test_weekly_html_escape(mocker, monkeypatch):
    """Dynamic fields with <, >, & must be HTML-escaped."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=7, month="2026-06")
    # Inject special chars in slug + content of first entry (frozen dataclass — replace)
    plan.entries[0] = _dc_15p3.replace(
        plan.entries[0],
        slug="centry<test>&promo",
        content="Hello & welcome <user> to Forton Lab",
    )

    responses = [_stub_tg_response(i) for i in range(101, 110)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    send_weekly_split(plan, inline_keyboard=None)

    first_text = spy.call_args_list[0].kwargs["json"]["text"]
    # Raw chars must NOT appear; escaped variants MUST
    assert "centry<test>&promo" not in first_text
    assert "Hello & welcome <user>" not in first_text
    assert "&amp;" in first_text
    assert "&lt;" in first_text
    assert "&gt;" in first_text


def test_weekly_30day_count(mocker, monkeypatch):
    """30-day June 2026 plan → 5 ISO-week messages (weeks 23/24/25/26/27)."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    responses = [_stub_tg_response(i) for i in range(101, 110)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    ids = send_weekly_split(plan, inline_keyboard=None)
    assert len(ids) == 5
    assert spy.call_count == 5


def test_weekly_returns_message_ids(mocker, monkeypatch):
    """Returns list[int] in send order — IDs from TG response.result.message_id."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    responses = [_stub_tg_response(mid) for mid in (501, 502, 503, 504, 505)]
    mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    ids = send_weekly_split(plan, inline_keyboard=None)
    assert ids == [501, 502, 503, 504, 505]


def test_weekly_pause_between_messages_called(mocker, monkeypatch):
    """time.sleep(pause_between_s) called between sends, NOT after the last."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    responses = [_stub_tg_response(i) for i in range(101, 110)]
    mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    sleep_spy = mocker.patch("src.tg_nudge.time.sleep")

    send_weekly_split(plan, inline_keyboard=None, pause_between_s=0.5)

    # 5 messages → 4 sleeps (between 1-2, 2-3, 3-4, 4-5; not after 5)
    assert sleep_spy.call_count == 4
    for c in sleep_spy.call_args_list:
        assert c.args[0] == 0.5


def test_weekly_split_28day_february(mocker, monkeypatch):
    """28-day February: count == unique ISO-weeks in plan."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=28, month="2026-02")
    unique_weeks = len({e.date.isocalendar()[1] for e in plan.entries})
    responses = [_stub_tg_response(i) for i in range(101, 110)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    ids = send_weekly_split(plan, inline_keyboard=None)
    assert len(ids) == unique_weeks
    assert spy.call_count == unique_weeks


def test_weekly_html_in_text_uses_html_parse_mode(mocker, monkeypatch):
    """parse_mode=HTML and disable_web_page_preview=True for every weekly message."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")
    from src.tg_nudge import send_weekly_split

    plan = _make_plan(num_days=30, month="2026-06")
    responses = [_stub_tg_response(i) for i in range(101, 110)]
    spy = mocker.patch("src.tg_nudge.requests.post", side_effect=responses)
    mocker.patch("src.tg_nudge.time.sleep")

    send_weekly_split(plan, inline_keyboard=None)

    for c in spy.call_args_list:
        body = c.kwargs["json"]
        assert body["parse_mode"] == "HTML"
        assert body["disable_web_page_preview"] is True
        assert body["chat_id"] == "12345"
