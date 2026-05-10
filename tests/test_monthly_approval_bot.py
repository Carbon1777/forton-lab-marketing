"""Async unit tests for monthly_approval_bot — covers all 5 threats T-1.5-01..05.

Plan 015-04 Wave 2 — main bot module with 3 callback handlers + lifecycle.
All tests use the PTB-mock fixtures from conftest (Plan 015-01 deliverables):
  mock_owner_id, mock_query, mock_ctx, tmp_repo_with_draft_plan.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, Update, User
from telegram.error import BadRequest


pytestmark = pytest.mark.asyncio  # all tests in this file are async


def _set_callback_data_for_plan(mock_query, plan_path, action="approve"):
    """Helper: compute fresh sha8 for plan, stuff into mock_query.data."""
    from src.plan_writer import plan_sha8

    sha8 = plan_sha8(plan_path)
    mock_query.data = f"{action}:{sha8}"
    return sha8


# ============================================================
# _should_skip_polling — pre-flight short-circuit (sync helper)
# ============================================================


def test_should_skip_polling_no_plan_file(tmp_path, monkeypatch):
    """Plan file absent → return True (no work to do)."""
    from src import monthly_approval_bot as bot

    monkeypatch.setattr(bot, "_repo_root", lambda: tmp_path)
    assert bot._should_skip_polling() is True


def test_should_skip_polling_status_approved(tmp_path, monkeypatch):
    """Plan with status=approved → return True."""
    from src import monthly_approval_bot as bot

    month = dt.date.today().strftime("%Y-%m")
    plan = tmp_path / "plans" / f"monthly_plan_{month}.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nmonth: {month}\nstatus: approved\n---\nbody")
    monkeypatch.setattr(bot, "_repo_root", lambda: tmp_path)
    assert bot._should_skip_polling() is True


def test_should_skip_polling_status_draft_returns_false(tmp_path, monkeypatch):
    """Plan with status=draft → return False (must poll)."""
    from src import monthly_approval_bot as bot

    month = dt.date.today().strftime("%Y-%m")
    plan = tmp_path / "plans" / f"monthly_plan_{month}.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nmonth: {month}\nstatus: draft\n---\nbody")
    monkeypatch.setattr(bot, "_repo_root", lambda: tmp_path)
    assert bot._should_skip_polling() is False


def test_extract_month_from_path():
    """Helper extracts YYYY-MM from monthly_plan_{month}.md filename."""
    from src.monthly_approval_bot import _extract_month_from_path

    assert _extract_month_from_path(Path("plans/monthly_plan_2026-06.md")) == "2026-06"
    assert _extract_month_from_path(Path("/abs/plans/monthly_plan_2027-12.md")) == "2027-12"


# ============================================================
# _check_owner — T-1.5-01 (callback spam)
# ============================================================


async def test_callback_from_wrong_user_rejected(mock_ctx):
    """T-1.5-01: callback from non-owner silently rejected; no side effect."""
    from src.monthly_approval_bot import handle_callback

    attacker = User(id=99999, first_name="attacker", is_bot=False)
    q = MagicMock(spec=CallbackQuery)
    q.from_user = attacker
    q.data = "approve:deadbeef"
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()

    update = MagicMock(spec=Update)
    update.callback_query = q

    await handle_callback(update, mock_ctx)

    q.answer.assert_awaited()  # ack TG (we still answer to be polite)
    q.edit_message_reply_markup.assert_not_called()
    q.edit_message_text.assert_not_called()
    mock_ctx.application.stop_running.assert_not_called()


# ============================================================
# _verify_sha — T-1.5-04 (stale callback)
# ============================================================


async def test_stale_sha_callback_rejected(mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker):
    """T-1.5-04: callback_data sha != current sha → reject + remove buttons."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    mock_query.data = "approve:00000000"  # deliberately stale
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )
    mock_approve = mocker.patch("src.monthly_approval_bot.plan_writer.approve_plan")

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_query.edit_message_text.assert_awaited()
    mock_approve.assert_not_called()
    mock_ctx.application.stop_running.assert_not_called()


# ============================================================
# _handle_approve — APPROVE-02 happy path + T-1.5-03 idempotency
# ============================================================


async def test_approve_callback_calls_plan_writer(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker
):
    """Happy path: valid owner + valid sha8 → approve_plan called → stop_running."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "approve")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )
    mock_approve = mocker.patch(
        "src.monthly_approval_bot.plan_writer.approve_plan",
        return_value="commit_xyz_1234567",
    )
    update = MagicMock(spec=Update)
    update.callback_query = mock_query

    await handle_callback(update, mock_ctx)

    mock_query.answer.assert_awaited_once()
    mock_query.edit_message_reply_markup.assert_awaited_once()
    mock_approve.assert_called_once()
    mock_query.message.reply_text.assert_awaited()
    reply_text = mock_query.message.reply_text.await_args.args[0]
    assert (
        "commit_x" in reply_text
        or "commit_xy" in reply_text
        or "Утверждено" in reply_text
    )
    mock_ctx.application.stop_running.assert_called_once()


async def test_buttons_removed_before_side_effect(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker
):
    """T-1.5-03: edit_message_reply_markup(None) called BEFORE approve_plan."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "approve")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )

    call_order = []
    mock_query.edit_message_reply_markup = AsyncMock(
        side_effect=lambda **kw: call_order.append("buttons_removed")
    )

    def fake_approve(*a, **kw):
        call_order.append("approve_plan")
        return "sha"

    mocker.patch(
        "src.monthly_approval_bot.plan_writer.approve_plan",
        side_effect=fake_approve,
    )

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    assert call_order.index("buttons_removed") < call_order.index("approve_plan")


async def test_double_callback_idempotent(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker
):
    """T-1.5-03: second tap → BadRequest 'message is not modified' → bail without 2nd approve."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "approve")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )
    mock_query.edit_message_reply_markup = AsyncMock(
        side_effect=BadRequest("Message is not modified")
    )
    mock_approve = mocker.patch("src.monthly_approval_bot.plan_writer.approve_plan")

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_approve.assert_not_called()
    mock_ctx.application.stop_running.assert_not_called()


async def test_409_on_approve_handled(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker
):
    """approve_plan raises GitHubAPIError 409 → user-facing message, no crash, no stop_running."""
    from src.monthly_approval_bot import handle_callback
    from src.plan_writer import GitHubAPIError

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "approve")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )
    mocker.patch(
        "src.monthly_approval_bot.plan_writer.approve_plan",
        side_effect=GitHubAPIError("PUT path -> 409 Conflict"),
    )

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_query.message.reply_text.assert_awaited()
    text = mock_query.message.reply_text.await_args.args[0]
    assert (
        "изменил" in text.lower()
        or "conflict" in text.lower()
        or "409" in text
        or "preview" in text.lower()
    )
    mock_ctx.application.stop_running.assert_not_called()


# ============================================================
# _handle_edit — APPROVE-03
# ============================================================


async def test_edit_handler_sends_reminder(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker
):
    """Edit callback → buttons removed + reply with relative plan path."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "edit")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_query.edit_message_reply_markup.assert_awaited()
    mock_query.message.reply_text.assert_awaited()
    text = mock_query.message.reply_text.await_args.args[0]
    rel = str(plan_path.relative_to(repo_root))
    assert rel in text
    mock_ctx.application.stop_running.assert_called_once()


# ============================================================
# _handle_reject — APPROVE-04 + APPROVE-05 + T-1.5-02
# ============================================================


async def test_reject_under_limit_dispatches(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker, monkeypatch
):
    """regen_count=1 < 3 → dispatch_regenerate called with month + force_regenerate=true."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "reject")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )

    spend = repo_root / ".metrics" / "api_spend.json"
    spend.parent.mkdir(exist_ok=True)
    spend.write_text(
        json.dumps(
            {
                "_schema_version": 2,
                "2026-06": {
                    "regen_count": 1,
                    "calls": 2,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "usd": 0.18,
                },
                "regen_limit_per_month": 3,
            }
        )
    )
    monkeypatch.setenv("BOT_DISPATCH_PAT", "test_pat")
    mocker.patch(
        "src.monthly_approval_bot._extract_month_from_path",
        return_value="2026-06",
    )
    mock_dispatch = mocker.patch(
        "src.monthly_approval_bot.plan_writer.dispatch_regenerate"
    )

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["inputs"]["month"] == "2026-06"
    assert kwargs["inputs"]["force_regenerate"] == "true"
    mock_ctx.application.stop_running.assert_called_once()


async def test_regen_limit_blocks_4th_attempt(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker
):
    """T-1.5-02: regen_count >= 3 → block, no dispatch."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "reject")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )

    spend = repo_root / ".metrics" / "api_spend.json"
    spend.parent.mkdir(exist_ok=True)
    spend.write_text(
        json.dumps(
            {
                "_schema_version": 2,
                "2026-06": {
                    "regen_count": 3,
                    "calls": 4,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "usd": 0.36,
                },
                "regen_limit_per_month": 3,
            }
        )
    )
    mocker.patch(
        "src.monthly_approval_bot._extract_month_from_path",
        return_value="2026-06",
    )
    mock_dispatch = mocker.patch(
        "src.monthly_approval_bot.plan_writer.dispatch_regenerate"
    )

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_dispatch.assert_not_called()
    mock_query.message.reply_text.assert_awaited()
    text = mock_query.message.reply_text.await_args.args[0]
    assert (
        "лимит" in text.lower()
        or "limit" in text.lower()
        or "3/3" in text
        or "исчерпан" in text.lower()
    )


async def test_reject_without_pat_replies_error(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker, monkeypatch
):
    """No BOT_DISPATCH_PAT → reply error, no dispatch, no stop_running."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "reject")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )
    monkeypatch.delenv("BOT_DISPATCH_PAT", raising=False)
    mocker.patch(
        "src.monthly_approval_bot._extract_month_from_path",
        return_value="2026-06",
    )
    mock_dispatch = mocker.patch(
        "src.monthly_approval_bot.plan_writer.dispatch_regenerate"
    )

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_dispatch.assert_not_called()
    mock_query.message.reply_text.assert_awaited()
    text = mock_query.message.reply_text.await_args.args[0]
    assert "BOT_DISPATCH_PAT" in text or "PAT" in text


# ============================================================
# T-1.5-05 — Token leak
# ============================================================


async def test_no_token_in_logs(
    mock_query, mock_ctx, tmp_repo_with_draft_plan, mocker, monkeypatch, capsys
):
    """T-1.5-05: bot must never print TG token or PAT format to stderr/stdout."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    _set_callback_data_for_plan(mock_query, plan_path, "approve")
    mocker.patch(
        "src.monthly_approval_bot._resolve_plan_path",
        return_value=plan_path,
    )

    monkeypatch.setenv(
        "BOT_DISPATCH_PAT", "github_pat_11AABBCCDD0123456789xxxxxxxxxxxxxxxx"
    )
    monkeypatch.setenv(
        "TG_PLANNER_BOT_TOKEN", "1234567890:AAEhBP0av-AbcDefGhiJklMnoPqrStuVwxYz"
    )

    mocker.patch(
        "src.monthly_approval_bot.plan_writer.approve_plan",
        return_value="abc1234",
    )
    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert not re.search(r"github_pat_[A-Za-z0-9]{10,}", combined), (
        f"PAT leaked to stderr/stdout: {combined!r}"
    )
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", combined), (
        f"TG token leaked to stderr/stdout: {combined!r}"
    )


# ============================================================
# Bad callback_data
# ============================================================


async def test_bad_callback_data_handled(
    mock_query, mock_ctx, tmp_repo_with_draft_plan
):
    """Malformed callback_data (no ':') → polite error, no crash, no stop_running."""
    from src.monthly_approval_bot import handle_callback

    repo_root, plan_path = tmp_repo_with_draft_plan
    mock_ctx.application.bot_data["repo_root"] = repo_root
    mock_query.data = "garbage_no_colon"

    update = MagicMock(spec=Update)
    update.callback_query = mock_query
    await handle_callback(update, mock_ctx)

    mock_query.edit_message_text.assert_awaited()
    mock_ctx.application.stop_running.assert_not_called()


# ============================================================
# Module-level constants & exports
# ============================================================


def test_module_exports_required_symbols():
    """All symbols from spec exist & POLL_TIMEOUT_S = 540."""
    from src import monthly_approval_bot as bot

    required = [
        "main",
        "build_application",
        "handle_callback",
        "_check_owner",
        "_verify_sha",
        "_should_skip_polling",
        "_handle_approve",
        "_handle_edit",
        "_handle_reject",
        "_resolve_plan_path",
        "_extract_month_from_path",
        "POLL_TIMEOUT_S",
    ]
    for sym in required:
        assert hasattr(bot, sym), f"missing public symbol: {sym}"
    assert bot.POLL_TIMEOUT_S == 540
