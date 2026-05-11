"""preview_watchdog — Phase 2.5 GH cron throttling guard.

Scheduled at 09:30 and 11:30 UTC (= 12:30 / 14:30 МСК). Checks today's
plan entry: if status=draft AND no preview_message_id in plan frontmatter
AND no drafts/<slug>.md → preview_bot's 12:00 МСК cron tick was throttled
by GitHub (known issue). Action:

    1. Trigger preview_bot.yml via workflow_dispatch (GH REST API).
    2. Send alert to «Планировщик» via tg_nudge — "watchdog: cron throttled,
       triggered preview_bot manually at HH:MM МСК".

Exits 0 always (watchdog must not fail the workflow run; alert is the signal).

Env (same as preview_bot.yml):
    TG_PLANNER_BOT_TOKEN, TG_OWNER_CHAT_ID, BOT_DISPATCH_PAT,
    REPO_OWNER, REPO_NAME.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import requests

from src import tg_nudge
from src.plan_reader import get_today_entries, parse_plan
from src.preview_bot import DRAFTS_DIR_NAME, PLANS_DIR_NAME, _today_msk

REPO_ROOT: Path = Path.cwd()


def _trigger_preview_bot() -> tuple[bool, str]:
    """POST /workflows/preview_bot.yml/dispatches. Returns (ok, error_msg)."""
    pat = os.environ.get("BOT_DISPATCH_PAT") or ""
    owner = os.environ.get("REPO_OWNER") or ""
    repo = os.environ.get("REPO_NAME") or ""
    if not (pat and owner and repo):
        return False, "missing BOT_DISPATCH_PAT/REPO_OWNER/REPO_NAME"
    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/workflows/preview_bot.yml/dispatches"
    )
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=10,
        )
        if r.status_code in (204, 200):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    today = _today_msk()
    plans_dir = REPO_ROOT / PLANS_DIR_NAME
    drafts_dir = REPO_ROOT / DRAFTS_DIR_NAME
    plan_path = plans_dir / f"monthly_plan_{today.strftime('%Y-%m')}.md"

    if not plan_path.exists():
        sys.stderr.write(f"INFO: no plan {plan_path.name} — nothing to watch\n")
        return 0

    try:
        plan = parse_plan(plan_path)
    except Exception as exc:
        sys.stderr.write(f"ERROR: parse_plan failed: {exc!r}\n")
        return 0   # watchdog never fails

    entries = get_today_entries(plan, today)
    if not entries:
        sys.stderr.write(f"INFO: no entries for {today.isoformat()}\n")
        return 0

    # Look for entries still pending preview (status=draft AND no draft file).
    # Skip approved/published/skipped/expired — preview already happened or
    # operator already saw it.
    missing: list[str] = []
    for entry in entries:
        if entry.status not in ("draft", None, ""):
            continue
        draft_path = drafts_dir / f"{entry.slug}.md"
        if draft_path.exists():
            continue   # preview already sent (draft cached)
        missing.append(entry.slug)

    if not missing:
        sys.stderr.write(f"INFO: all entries for {today.isoformat()} OK\n")
        return 0

    sys.stderr.write(
        f"WARN: GH cron throttled — missing preview for: {missing}; "
        f"triggering preview_bot.yml\n"
    )

    ok, err = _trigger_preview_bot()
    if not ok:
        sys.stderr.write(f"ERROR: dispatch failed: {err}\n")
        # Still alert operator so they can trigger manually
        _alert(missing, dispatch_ok=False, dispatch_err=err)
        return 0

    _alert(missing, dispatch_ok=True, dispatch_err="")
    return 0


def _alert(missing: list[str], *, dispatch_ok: bool, dispatch_err: str) -> None:
    """Send tg_nudge alert; gracefully degrade if template missing."""
    now_msk = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).strftime("%H:%M")
    slugs = ", ".join(missing)
    status_line = (
        f"✅ <code>preview_bot.yml</code> dispatched"
        if dispatch_ok
        else f"⚠️ dispatch failed: <code>{dispatch_err[:120]}</code>"
    )
    text = (
        f"🐕 <b>Watchdog: preview_bot cron throttled</b>\n"
        f"Время проверки: {now_msk} МСК\n"
        f"Без preview на сегодня: <code>{slugs}</code>\n"
        f"{status_line}\n"
        f"<i>GH Actions scheduled workflows иногда пропускают тики при высокой "
        f"нагрузке — это известное поведение, не баг pipeline. Watchdog "
        f"восстанавливает.</i>"
    )
    # tg_nudge.send требует template_key — отправляем raw через api
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — skip alert\n")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        sys.stderr.write(f"WARN: alert send failed: {exc!r}\n")


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
