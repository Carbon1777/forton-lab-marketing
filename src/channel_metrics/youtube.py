"""YouTube-канал: публичная статистика через Data API v3 channels.list.

ОПЦИОНАЛЬНО. Существующий YT OAuth refresh_token имеет scope только
youtube.upload и НЕ МОЖЕТ читать статистику. Поэтому берём публичные метрики
через API-ключ (YT_API_KEY, бесплатный Data API key) + YT_CHANNEL_ID.

Если YT_API_KEY/YT_CHANNEL_ID не заданы — возвращаем error-снапшот
(«optional»), дайджест рендерит строку YouTube как «нет данных» и не падает.
Никакого блокера: студия может добавить бесплатный ключ позже.
"""
from __future__ import annotations

import datetime as dt
import os

from ._http import fetch_with_retry
from .models import ChannelSnapshot

YT_CHANNELS_API = "https://www.googleapis.com/youtube/v3/channels"


def fetch_weekly(week_start: dt.date) -> ChannelSnapshot:
    api_key = os.environ.get("YT_API_KEY")
    channel_id = os.environ.get("YT_CHANNEL_ID")
    if not (api_key and channel_id):
        return ChannelSnapshot(
            platform="youtube", week_start=week_start, subscribers=None,
            error="YT_API_KEY/YT_CHANNEL_ID not set (optional)",
        )
    try:
        resp = fetch_with_retry(
            YT_CHANNELS_API,
            method="GET",
            params={"part": "statistics", "id": channel_id, "key": api_key},
        )
        data = resp.json()
        items = data.get("items") or []
        if not items:
            return ChannelSnapshot(
                platform="youtube", week_start=week_start, subscribers=None,
                error="YT: channel not found",
            )
        stats = items[0].get("statistics", {})
        subs = stats.get("subscriberCount")
        # hiddenSubscriberCount=True → поле отсутствует; тогда subscribers=None.
        return ChannelSnapshot(
            platform="youtube", week_start=week_start,
            subscribers=int(subs) if subs is not None else None,
        )
    except Exception as exc:  # noqa: BLE001
        return ChannelSnapshot(
            platform="youtube", week_start=week_start, subscribers=None,
            error=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
