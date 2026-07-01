"""Telegram-канал: число подписчиков через Bot API getChatMemberCount.

Ноль новых секретов: переиспользует TG_BOT_TOKEN (бот @fortonlab_bot уже
админ в @fortonlab). Канал берётся из TG_STATS_CHANNEL (default '@fortonlab').

Через Bot API доступно ТОЛЬКО число подписчиков — просмотры постов/охват
требуют MTProto/TGStat, что запрещено ban-risk констрейнтом и в scope не входит.
"""
from __future__ import annotations

import datetime as dt
import os

from ._http import fetch_with_retry
from .models import ChannelSnapshot

TG_API = "https://api.telegram.org"
DEFAULT_CHANNEL = "@fortonlab"


def fetch_weekly(week_start: dt.date) -> ChannelSnapshot:
    token = os.environ.get("TG_BOT_TOKEN")
    # GH Actions отдаёт ОТСУТСТВУЮЩИЙ секрет как пустую строку (не unset), поэтому
    # os.environ.get(name, default) не сработает — используем `or DEFAULT`.
    channel = os.environ.get("TG_STATS_CHANNEL") or DEFAULT_CHANNEL
    if not token:
        return ChannelSnapshot(
            platform="telegram", week_start=week_start,
            subscribers=None, error="TG_BOT_TOKEN not set",
        )
    try:
        resp = fetch_with_retry(
            f"{TG_API}/bot{token}/getChatMemberCount",
            method="GET",
            params={"chat_id": channel},
        )
        data = resp.json()
        if not data.get("ok"):
            return ChannelSnapshot(
                platform="telegram", week_start=week_start, subscribers=None,
                error=f"TG API: {str(data.get('description'))[:80]}",
            )
        return ChannelSnapshot(
            platform="telegram", week_start=week_start,
            subscribers=int(data["result"]),
        )
    except Exception as exc:  # noqa: BLE001 — fetcher NEVER raises, degrade to error snap
        return ChannelSnapshot(
            platform="telegram", week_start=week_start, subscribers=None,
            error=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
