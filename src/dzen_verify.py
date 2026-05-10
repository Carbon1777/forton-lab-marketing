"""marketing-v3/src/dzen_verify.py — Дзен manual-check reminder (PUB-10, scope-downgraded).

Phase 2. Called by publish.yml workflow step через 5-10 мин после публикации в TG.
Шлёт TG-нудж в «Планировщик» с напоминанием вручную проверить что @zen_sync_bot
подтянул пост на dzen.ru/fortonlab.

Strategy: TG-reminder (1 клик в браузере у юзера, который залогинен в Yandex).

Why NOT HTTP-scrape (original Plan 02-03 strategy): Yandex Дзен в 2024-2025 закрыл
публичный анонимный доступ к каналам. ВСЕ probe-варианты упёрлись в SSO redirect:
    - GET https://dzen.ru/fortonlab (desktop UA) → 302 → sso.passport.yandex.ru
    - Mobile UA → тот же SSO
    - /rss endpoint → тот же SSO
    - /api/v3/launcher/more?channel_name=fortonlab → 200 но items: [] без cookie
    - zen-rss.ru third-party proxy → DNS/connection fail (сервис мёртв)

Cookie-based fetch отвергнут: cookie протухает за 30-90 дней, requires manual rotation.
Не оправдано для вторичного канала (Дзен у нас cross-post из TG).

Зафиксировано: Brain/projects/forton-lab/decisions.md (запись 2026-05-10).

Threat-model anchors:
    T-2-08-A — verify-style alert через tg_nudge с specific reminder text
    T-2-08-B — exit 0 always — workflow continues even on TG-nudge failure

Public API:
    verify(slug) -> bool          # always True after sending reminder
    DZEN_CHANNEL_URL: str
    REMINDER_WAIT_MIN: int = 10

CLI:
    python -m src.dzen_verify <slug>   # always exits 0
"""
from __future__ import annotations

import sys
from typing import Final

DZEN_CHANNEL_URL: Final[str] = "https://dzen.ru/fortonlab"
REMINDER_WAIT_MIN: Final[int] = 10   # юзер откроет dzen вручную через 10 мин


def verify(slug: str = "") -> bool:
    """Send TG manual-check reminder to «Планировщик»; return True on send OK.

    Never raises (TG outage логируется, swallowed) — silent-fail nonblocking.
    """
    return _send_reminder(slug or "(unknown)")


def _send_reminder(slug: str) -> bool:
    """Send the dzen_manual_check tg_nudge. Returns True on success, False on TG fail."""
    try:
        from src import tg_nudge
        tg_nudge.send(
            "dzen_manual_check",
            slug=slug,
            channel_url=DZEN_CHANNEL_URL,
            wait_min=REMINDER_WAIT_MIN,
        )
        return True
    except Exception as exc:
        sys.stderr.write(f"WARN: tg_nudge failed during dzen reminder: {exc!r}\n")
        return False


if __name__ == "__main__":
    cli_slug = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        verify(cli_slug)
    except Exception as exc:
        # Last-resort safety net — never escape with non-zero
        sys.stderr.write(f"WARN: verify raised unexpectedly: {exc!r}\n")
    sys.exit(0)   # silent-fail nonblocking invariant
