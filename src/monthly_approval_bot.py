"""monthly_approval_bot — TG bot for approving / editing / rejecting monthly plan.

Phase 1.5 Plan 015-04 deliverable. Polling-in-window architecture (D-1.5-04):
GH Actions cron triggers this script every 10 minutes during 1-3 of each month.
On each invocation:
    1. _should_skip_polling() — pre-flight short-circuit. If the current month plan
       file is missing or already approved, exit 0 immediately (≤ 5s of CI runtime).
    2. Otherwise build PTB Application + register handle_callback as the only
       CallbackQueryHandler, then run_polling for POLL_TIMEOUT_S=540 (9 min).
    3. The first valid callback (approve/edit/reject completed) signals
       application.stop_running() — workflow exits cleanly.

Threat-model anchors (T-1.5-01..05):
    T-1.5-01 — _check_owner is the FIRST gate after query.answer(). Non-owner
               callbacks are answered then dropped silently.
    T-1.5-02 — _handle_reject preflights regen_count vs limit; over → block.
    T-1.5-03 — query.edit_message_reply_markup(reply_markup=None) is called
               BEFORE any side-effect; double-tap raises BadRequest, we bail.
               409 from approve_plan triggers a soft-fail (no stop_running)
               so the user can retry from a fresh preview.
    T-1.5-04 — _verify_sha recomputes plan_sha8 of the current file and compares
               to the sha8 embedded in callback_data. Mismatch → reject with
               explainer.
    T-1.5-05 — env vars are read but NEVER printed. All log lines refer to
               public ids (chat_id) or sanitised paths.

Public API (used by tests + workflow):
    main, build_application, handle_callback, _check_owner, _verify_sha,
    _should_skip_polling, _handle_approve, _handle_edit, _handle_reject,
    _resolve_plan_path, _extract_month_from_path, POLL_TIMEOUT_S
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import sys
from pathlib import Path

import frontmatter
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    Defaults,
)

from src import plan_writer

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

POLL_TIMEOUT_S = 540  # 9 min; leaves 1-min headroom before GH 10-min cron tick
PLANS_DIR_NAME = "plans"
SPEND_FILE_REL = ".metrics/api_spend.json"
_PLAN_FILENAME_RE = re.compile(r"monthly_plan_(\d{4}-\d{2})\.md$")


# ---------------------------------------------------------------------------
# Soft-fail signal — graceful handler exit WITHOUT stop_running
# ---------------------------------------------------------------------------


class _SoftFail(Exception):
    """Raised by a handler to signal handle_callback NOT to call stop_running.

    Used when a side-effect failed in a recoverable way (e.g. 409 conflict on
    approve) — the user can retry from a fresh preview, so we keep polling.
    """


# ---------------------------------------------------------------------------
# Path helpers (test-patchable)
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return marketing-v3/ root. Test-patchable."""
    return Path(__file__).resolve().parent.parent


def _resolve_plan_path(ctx: ContextTypes.DEFAULT_TYPE | object) -> Path:
    """Compute path to the current month's plan file (using bot_data['repo_root'])."""
    repo_root = ctx.application.bot_data["repo_root"]
    month = dt.date.today().strftime("%Y-%m")
    return Path(repo_root) / PLANS_DIR_NAME / f"monthly_plan_{month}.md"


def _extract_month_from_path(plan_path: Path) -> str:
    """Parse 'monthly_plan_YYYY-MM.md' filename → 'YYYY-MM' string.

    Falls back to today's month if the filename does not match the canonical
    pattern (defensive — should never happen with our cron).
    """
    m = _PLAN_FILENAME_RE.search(str(plan_path))
    if m:
        return m.group(1)
    return dt.date.today().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Pre-flight: short-circuit if nothing to approve
# ---------------------------------------------------------------------------


def _should_skip_polling() -> bool:
    """Return True if this cron-tick should exit immediately (no polling).

    True when:
        - current month plan file missing
        - plan file frontmatter has status=approved

    False when:
        - plan file exists with status=draft (or anything other than approved)
        - frontmatter cannot be parsed (defensive — poll anyway, surface error
          via callback handlers)
    """
    month = dt.date.today().strftime("%Y-%m")
    plan_path = _repo_root() / PLANS_DIR_NAME / f"monthly_plan_{month}.md"
    if not plan_path.exists():
        return True
    try:
        post = frontmatter.load(plan_path)
        status = post.metadata.get("status")
    except Exception as exc:  # noqa: BLE001 — defensive; corrupt file = bot's job to retry
        sys.stderr.write(
            f"WARN: cannot parse plan {plan_path.name} ({exc!r}); polling anyway\n"
        )
        return False
    return status == "approved"


# ---------------------------------------------------------------------------
# Owner check — T-1.5-01
# ---------------------------------------------------------------------------


async def _check_owner(query, ctx) -> bool:
    """Return True iff query.from_user.id matches bot_data['owner_chat_id']."""
    owner = ctx.application.bot_data["owner_chat_id"]
    if query.from_user.id != owner:
        # We still answer the callback to avoid keeping the spinner spinning
        # for the attacker (no information leak — they already know the chat_id
        # since they sent the callback).
        try:
            await query.answer("Not authorized", show_alert=False)
        except Exception:  # noqa: BLE001 — best-effort
            pass
        sys.stderr.write(
            f"WARN: callback from non-owner user_id={query.from_user.id} "
            f"(expected {owner})\n"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Anti-replay sha verification — T-1.5-04
# ---------------------------------------------------------------------------


async def _verify_sha(query, plan_path: Path, expected_sha8: str) -> bool:
    """Compare current plan_sha8 with sha8 from callback_data.

    Returns True iff the file exists AND its current sha8 equals expected.
    On mismatch (or missing file), edits the message to remove buttons and
    explain that the plan has changed — caller MUST return immediately.
    """
    if not plan_path.exists():
        try:
            await query.edit_message_text(
                "⚠️ <b>План отсутствует</b>\nФайл удалён или ещё не сгенерирован."
            )
        except BadRequest:
            pass
        return False
    actual = plan_writer.plan_sha8(plan_path)
    if actual != expected_sha8:
        try:
            await query.edit_message_text(
                f"⚠️ <b>План был обновлён</b> ({expected_sha8} → {actual})\n"
                "Жди новый preview с актуальными кнопками."
            )
        except BadRequest:
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# _handle_approve — APPROVE-02
# ---------------------------------------------------------------------------


async def _handle_approve(query, ctx, plan_path: Path) -> None:
    """Mutate frontmatter to status=approved + commit via GH API; reply with sha."""
    month = _extract_month_from_path(plan_path)
    repo_root = ctx.application.bot_data["repo_root"]
    try:
        commit_sha = await asyncio.to_thread(
            plan_writer.approve_plan,
            plan_path=plan_path,
            repo_root=repo_root,
            month=month,
            approver="forton-via-tg-bot",
        )
    except plan_writer.GitHubAPIError as exc:
        if "409" in str(exc):
            await query.message.reply_text(
                "⚠️ <b>План изменился во время одобрения</b>\n"
                "Жди новый preview через 1-3 минуты."
            )
            raise _SoftFail() from exc
        raise

    msk_now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).strftime(
        "%H:%M МСК"
    )
    short_sha = commit_sha[:7] if commit_sha else "n/a"
    await query.message.reply_text(
        f"✅ <b>Утверждено в {msk_now}</b>\n"
        f"Commit: <code>{short_sha}</code>\n\n"
        f"Phase 2 daily generator подхватит «сегодня» по дате."
    )


# ---------------------------------------------------------------------------
# _handle_edit — APPROVE-03
# ---------------------------------------------------------------------------


async def _handle_edit(query, ctx, plan_path: Path) -> None:
    """Send reminder with relative path to the plan file (no side effects)."""
    repo_root = ctx.application.bot_data["repo_root"]
    rel = plan_path.relative_to(repo_root)
    await query.message.reply_text(
        f"✏️ <b>Открой и правь</b>\n"
        f"Файл: <code>{rel}</code>\n"
        f"После правок: commit + push в <code>main</code> — Phase 2 daily generator "
        f"подхватит «сегодня» по дате.\n\n"
        f"<i>Кнопки устарели — статус остаётся <code>draft</code>. "
        f"Чтобы пересоздать preview с новыми sha — дёрни workflow "
        f"<code>monthly_approval_bot</code>.</i>"
    )


# ---------------------------------------------------------------------------
# _handle_reject — APPROVE-04 + APPROVE-05 + T-1.5-02
# ---------------------------------------------------------------------------


async def _handle_reject(query, ctx, plan_path: Path) -> None:
    """Check regen_count vs limit; if under, dispatch monthly_plan workflow."""
    repo_root = ctx.application.bot_data["repo_root"]
    month = _extract_month_from_path(plan_path)
    spend_file = Path(repo_root) / SPEND_FILE_REL

    used = await asyncio.to_thread(plan_writer.read_regen_count, spend_file, month)
    limit = await asyncio.to_thread(plan_writer.read_regen_limit, spend_file)

    if used >= limit:
        await query.message.reply_text(
            f"❌ <b>Лимит regenerate ({limit}) исчерпан для {month}</b>\n"
            f"Уже использовано: {used}/{limit}.\n"
            f"Открой план и правь руками: "
            f"<code>{plan_path.relative_to(repo_root)}</code>"
        )
        raise _SoftFail()  # don't stop_running — keep polling for edit/approve

    pat = os.environ.get("BOT_DISPATCH_PAT")
    if not pat:
        await query.message.reply_text(
            "⚠️ Env <code>BOT_DISPATCH_PAT</code> отсутствует — "
            "не могу запустить workflow"
        )
        raise _SoftFail()

    owner = os.environ.get("REPO_OWNER", "Carbon1777")
    repo = os.environ.get("REPO_NAME", "forton-lab-marketing")
    await asyncio.to_thread(
        plan_writer.dispatch_regenerate,
        pat=pat,
        owner=owner,
        repo=repo,
        workflow="monthly_plan.yml",
        ref="main",
        inputs={"month": month, "force_regenerate": "true"},
    )
    await query.message.reply_text(
        f"🔁 <b>Запущена регенерация {used + 1}/{limit}</b>\n"
        f"Workflow <code>monthly_plan</code> создаст новый план через ~2 минуты. "
        f"Жди новый preview в этом канале."
    )


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Single CallbackQueryHandler for all 3 actions (approve/edit/reject).

    Order is significant — see threat-model docstring at module top:
        1. answer (TG ack within 15 s)
        2. _check_owner (T-1.5-01)
        3. parse callback_data (bad → polite error)
        4. _verify_sha (T-1.5-04)
        5. edit_message_reply_markup(None) — idempotency lock (T-1.5-03)
        6. dispatch to _handle_<action>
        7. on success → application.stop_running()
        8. on _SoftFail → return (no stop_running)
    """
    query = update.callback_query
    try:
        await query.answer()
    except Exception:  # noqa: BLE001 — never crash on TG ack failure
        pass

    if not await _check_owner(query, ctx):
        return

    data = query.data or ""
    try:
        action, sha8 = data.split(":", 1)
    except ValueError:
        try:
            await query.edit_message_text(f"⚠️ Bad callback: {data!r}")
        except BadRequest:
            pass
        return

    plan_path = _resolve_plan_path(ctx)
    if not await _verify_sha(query, plan_path, sha8):
        return

    # Idempotency lock — strip buttons BEFORE side-effect (T-1.5-03)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            sys.stderr.write(
                "INFO: duplicate callback (buttons already removed); bailing\n"
            )
            return
        raise

    try:
        if action == "approve":
            await _handle_approve(query, ctx, plan_path)
        elif action == "edit":
            await _handle_edit(query, ctx, plan_path)
        elif action == "reject":
            await _handle_reject(query, ctx, plan_path)
        else:
            try:
                await query.message.reply_text(f"⚠️ Unknown action: {action!r}")
            except Exception:  # noqa: BLE001
                pass
            return
    except _SoftFail:
        return  # graceful — keep polling

    try:
        ctx.application.stop_running()
    except Exception as exc:  # noqa: BLE001 — log but don't crash mid-shutdown
        sys.stderr.write(f"WARN: stop_running() failed: {exc!r}\n")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def build_application(token: str) -> Application:
    """Construct PTB Application with HTML defaults + single callback handler."""
    defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    app = (
        Application.builder()
        .token(token)
        .defaults(defaults)
        .concurrent_updates(False)
        .build()
    )
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    """Read env var or exit 1 with stderr message (no value leak)."""
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: env {name} is missing\n")
        sys.exit(1)
    return val


def main() -> int:
    """Entry point for `python -m src.monthly_approval_bot`.

    Sequence:
        1. _should_skip_polling — if True, exit 0 immediately
        2. Build Application; populate bot_data with owner_chat_id + repo_root
        3. run_polling(timeout=POLL_TIMEOUT_S, drop_pending_updates=False)
        4. On stop_running() (from successful callback) — return 0
    """
    token = _env("TG_PLANNER_BOT_TOKEN")
    # TG_OWNER_USER_ID = personal Telegram user_id (positive, validated against callback_query.from_user.id)
    # TG_OWNER_CHAT_ID = channel/chat_id where bot sends messages (often negative for channels) — different value!
    # Fallback to TG_OWNER_CHAT_ID for backward-compat if TG_OWNER_USER_ID not set.
    import os
    owner_chat_id = int(os.environ.get("TG_OWNER_USER_ID") or _env("TG_OWNER_CHAT_ID"))

    if _should_skip_polling():
        sys.stderr.write("OK: nothing to approve, exiting\n")
        return 0

    app = build_application(token)
    app.bot_data["owner_chat_id"] = owner_chat_id
    app.bot_data["repo_root"] = _repo_root()

    app.run_polling(
        poll_interval=2.0,
        timeout=POLL_TIMEOUT_S,
        allowed_updates=[Update.CALLBACK_QUERY],
        drop_pending_updates=False,
        close_loop=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
