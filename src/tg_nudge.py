"""Forton Lab — Telegram nudges for monthly plan workflow events.

Sends HTML-formatted messages to the personal "Планировщик" channel for the
4 events emitted by ``monthly_plan_generator.py`` (Phase 1 PLAN-04):

    - ``monthly_plan_success``         — план готов; ссылка на коммит и инструкция
    - ``monthly_plan_failure``         — Anthropic API outage / unexpected error
    - ``monthly_plan_brand_violation`` — brand-safety lint hard-fail
    - ``monthly_plan_budget_cap``      — pre-flight budget cap reached

Reused by Phase 2 generators for similar events (templates can be added later).

Environment:
    TG_PLANNER_BOT_TOKEN  — bot token for @fortonlab_planner_bot
    TG_OWNER_CHAT_ID      — personal chat_id (numeric)

CLI usage (used by GH Actions workflow in Plan 05)::

    NUDGE_MONTH_RU="июня 2026"  NUDGE_PLAN_PATH=plans/x.md \\
        NUDGE_COMMIT_URL=...    NUDGE_COMMIT_SHA7=abc1234 \\
        NUDGE_ENTRIES_COUNT=30  NUDGE_USD_SPENT=0.07 \\
        python -m tg_nudge monthly_plan_success

All ``NUDGE_<KEY>`` env vars are stripped of the ``NUDGE_`` prefix and
lowercased to match template variable names.

Security & quality:
    - parse_mode=HTML, disable_web_page_preview=True (no preview cards leaking)
    - timeout=30s, ``raise_for_status`` propagates network errors
    - vars are internal data only (commit URLs, sha7, our own monetary numbers)
      — no untrusted input passes through .format(), so no HTML escape needed
"""
from __future__ import annotations

import html
import os
import sys
import time
from collections import defaultdict

import requests

TG_API_BASE = "https://api.telegram.org"


# --- Templates --------------------------------------------------------------
# Точные тексты из Phase 1 RESEARCH.md §«TG nudge template». Все 4 — на русском,
# все 4 — HTML-форматированные, все 4 — заканчиваются призывом к действию.

_T_SUCCESS = (
    "✅ <b>Месячный план для {month_ru} готов</b>\n"
    "\n"
    "Файл: <code>{plan_path}</code>\n"
    'Коммит: <a href="{commit_url}">{commit_sha7}</a>\n'
    "\n"
    "В плане: {entries_count} записей на каждый день месяца.\n"
    "Расход API: ${usd_spent} (входит в $5/мес cap).\n"
    "\n"
    "<b>Что делать дальше:</b>\n"
    "1. Открой файл в редакторе: <code>{plan_path}</code>\n"
    "2. Правь любые секции (≤15 мин)\n"
    "3. Закоммить и запушь — Phase 2 daily generator подхватит «сегодня» по дате\n"
    "\n"
    "⏰ Phase 2 cron: 12:00 МСК ежедневно."
)

_T_BRAND_VIOLATION = (
    "🚫 <b>Месячный план для {month_ru} НЕ СОЗДАН</b>\n"
    "\n"
    "Причина: brand-safety lint hard-fail.\n"
    "\n"
    "Сработавшие нарушения:\n"
    "{violations_list}"
    "\n"
    "<b>Что делать:</b>\n"
    '1. Запусти monthly_plan workflow вручную через GitHub UI: <a href="{actions_url}">Actions → monthly_plan → Run workflow</a>\n'
    "2. Если повторится — может Claude слишком привязалась к маркетинговому шаблону;\n"
    "   сходи в <code>src/monthly_plan_generator.py</code> и подкрути SYSTEM_PROMPT.\n"
    "\n"
    "Промпт сохранён в логе workflow для диагностики."
)

_T_BUDGET_CAP = (
    "⚠️ <b>Бюджет Claude API исчерпан</b>\n"
    "\n"
    "Текущий расход: ${usd_current} / ${usd_cap} cap.\n"
    "Месячный план для {month_ru} НЕ СОЗДАН — pre-flight отказал.\n"
    "\n"
    "<b>Что делать:</b>\n"
    "1. Дождись 1 числа следующего месяца — счётчик сбросится.\n"
    '2. Или подними cap в Anthropic Console: <a href="{console_url}">console.anthropic.com/settings/limits</a>\n'
    '3. Дёрни workflow повторно: <a href="{actions_url}">Actions → monthly_plan → Run workflow</a>'
)

_T_FAILURE = (
    "🔥 <b>Claude API недоступен</b>\n"
    "\n"
    "Месячный план для {month_ru} НЕ СОЗДАН.\n"
    "Причина: {reason}\n"
    "\n"
    "<b>Что делать:</b>\n"
    '1. Проверь статус: <a href="{status_url}">status.anthropic.com</a>\n'
    '2. Когда зелёный — дёрни workflow повторно: <a href="{actions_url}">Actions → monthly_plan → Run workflow</a>\n'
    "   (cron в следующий раз через месяц, ждать нельзя если хочешь план на текущий месяц)"
)

TEMPLATES: dict[str, str] = {
    "monthly_plan_success": _T_SUCCESS,
    "monthly_plan_failure": _T_FAILURE,
    "monthly_plan_brand_violation": _T_BRAND_VIOLATION,
    "monthly_plan_budget_cap": _T_BUDGET_CAP,
}


# --- env() helper (stiль tg_post.py) ---------------------------------------


def env(name: str) -> str:
    """Return env var value or exit 1 with stderr message."""
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: env {name} is missing\n")
        sys.exit(1)
    return val


# --- send() public API ------------------------------------------------------


def send(template_key: str, **vars: object) -> int:
    """Render ``TEMPLATES[template_key]`` with ``vars`` and POST to TG.

    Args:
        template_key: One of the 4 keys in :data:`TEMPLATES`. Raises ``KeyError``
            on unknown key — fail-fast, no silent default.
        **vars: Substitution variables for the template. Missing variables raise
            ``KeyError`` from ``str.format``.

    Returns:
        0 on success.

    Raises:
        SystemExit: if ``TG_PLANNER_BOT_TOKEN`` or ``TG_OWNER_CHAT_ID`` missing.
        requests.HTTPError: on non-2xx response from TG API.
        KeyError: on unknown ``template_key`` or missing format variable.
    """
    token = env("TG_PLANNER_BOT_TOKEN")
    chat_id = env("TG_OWNER_CHAT_ID")
    text = TEMPLATES[template_key].format(**vars)
    url = f"{TG_API_BASE}/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()
    return 0


# --- CLI entry --------------------------------------------------------------


def _main_from_argv(argv: list[str]) -> int:
    """Internal CLI entry — testable via direct call."""
    if len(argv) < 2:
        sys.stderr.write(
            "usage: python -m tg_nudge <template_key>\n"
            "  Reads NUDGE_<KEY> env vars for template substitution.\n"
        )
        return 1
    template_key = argv[1]
    nudge_vars = {
        k.removeprefix("NUDGE_").lower(): v
        for k, v in os.environ.items()
        if k.startswith("NUDGE_")
    }
    return send(template_key, **nudge_vars)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main_from_argv(sys.argv))


# =====================================================================
# Phase 1.5 Plan 03 — Multi-message weekly split + inline keyboard
# APPROVE-01. Added 2026-05-10.
# =====================================================================

_WEEK_HEADER = "📅 <b>Неделя {n} ({range_short})</b>\n\n"
_ENTRY_LINE = (
    "<b>{date_short}</b> <code>{slug}</code> ({channels})\n"
    "<i>{excerpt}</i>\n"
    "sha: <code>{media_sha8}</code>\n\n"
)
_EXCERPT_MAX = 120
_MESSAGE_LIMIT = 4096  # TG sendMessage text limit


def _render_week_message(week_num: int, entries: list) -> str:
    """Render one weekly TG-message in HTML format. All dynamic fields escaped.

    Truncates at ``_MESSAGE_LIMIT`` to guarantee TG accepts the message —
    worst case we drop the trailing entries (extremely unlikely with our
    per-day sizes, but safety net).
    """
    first = entries[0].date
    last = entries[-1].date
    range_short = f"{first.strftime('%d.%m')}-{last.strftime('%d.%m')}"
    parts = [_WEEK_HEADER.format(n=week_num, range_short=range_short)]
    for e in entries:
        excerpt_raw = (e.content or "").strip().replace("\n", " ")
        truncated = excerpt_raw[:_EXCERPT_MAX] + (
            "…" if len(excerpt_raw) > _EXCERPT_MAX else ""
        )
        excerpt = html.escape(truncated)
        slug_esc = html.escape(e.slug)
        channels_esc = html.escape(",".join(e.channels))
        media_sha8 = e.media[0].sha256[:8] if e.media else "—"
        parts.append(
            _ENTRY_LINE.format(
                date_short=e.date.strftime("%d.%m"),
                slug=slug_esc,
                channels=channels_esc,
                excerpt=excerpt,
                media_sha8=media_sha8,
            )
        )
    text = "".join(parts)
    if len(text) > _MESSAGE_LIMIT:
        text = text[: _MESSAGE_LIMIT - 1] + "…"
    return text


def send_weekly_split(
    plan,
    inline_keyboard: list | None = None,
    pause_between_s: float = 0.2,
) -> list[int]:
    """Send N TG-messages (one per ISO-week) for the monthly plan preview.

    APPROVE-01 implementation. Groups ``plan.entries`` by ISO-week, renders one
    HTML message per group, sends sequentially via raw TG Bot API.

    - ``reply_markup`` is attached ONLY to the LAST message (the one carrying
      the [✅ Утвердить] [✏️ Редактировать] [❌ Отклонить] buttons).
    - ``disable_notification=True`` for all but the LAST (silent preview pages,
      loud action page).
    - ``time.sleep(pause_between_s)`` between sends (rate-limit + ordering).

    Returns ``list[int]`` of TG message_ids in send order; the LAST element is
    the keyboard-bearing message_id which the bot edits on callback.
    """
    token = env("TG_PLANNER_BOT_TOKEN")
    chat_id = env("TG_OWNER_CHAT_ID")
    url = f"{TG_API_BASE}/bot{token}/sendMessage"

    # Group entries by ISO-week number (preserves date order — entries already
    # sorted by date in plan_reader.parse_plan_text).
    weeks: dict[int, list] = defaultdict(list)
    for e in plan.entries:
        weeks[e.date.isocalendar()[1]].append(e)
    sorted_weeks = [weeks[w] for w in sorted(weeks)]

    message_ids: list[int] = []
    last_idx = len(sorted_weeks) - 1
    for i, week_entries in enumerate(sorted_weeks):
        text = _render_week_message(i + 1, week_entries)
        body: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": (i != last_idx),
        }
        if i == last_idx and inline_keyboard:
            body["reply_markup"] = {"inline_keyboard": inline_keyboard}
        r = requests.post(url, json=body, timeout=30)
        r.raise_for_status()
        message_ids.append(int(r.json()["result"]["message_id"]))
        if i < last_idx:
            time.sleep(pause_between_s)

    return message_ids
