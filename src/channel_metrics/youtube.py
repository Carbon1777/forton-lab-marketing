"""YouTube-канал: подписчики — двумя путями, без новых обязательных секретов.

Путь 1 (по умолчанию): OAuth channels.list(mine=true, part=statistics) на
СУЩЕСТВУЮЩИХ секретах YT_CLIENT_ID/SECRET/REFRESH_TOKEN (те же, что
youtube_post.py). Сработает, если их scope пускает к чтению статистики своего
канала.

Путь 2 (fallback): публичная статистика через free YT_API_KEY + YT_CHANNEL_ID.

Если OAuth не отдал число и ключа нет (или ничего не сконфигурировано) —
error-снапшот («optional»): дайджест рендерит YouTube как «нет данных» и не
падает. Никакого блокера.
"""
from __future__ import annotations

import datetime as dt
import os

from ._http import fetch_with_retry
from .models import ChannelSnapshot

YT_CHANNELS_API = "https://www.googleapis.com/youtube/v3/channels"
YT_TOKEN_URI = "https://oauth2.googleapis.com/token"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _ok(week_start: dt.date, subs) -> ChannelSnapshot:
    return ChannelSnapshot(
        platform="youtube", week_start=week_start,
        subscribers=int(subs) if subs is not None else None,
    )


def _err(week_start: dt.date, msg: str) -> ChannelSnapshot:
    return ChannelSnapshot(
        platform="youtube", week_start=week_start, subscribers=None, error=msg,
    )


def _fetch_via_oauth(week_start: dt.date) -> ChannelSnapshot:
    # Ленивый импорт google-либ (как в store_metrics) — чтобы api-key путь и
    # тесты не тянули discovery без нужды.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri=YT_TOKEN_URI,
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=YT_SCOPES,
    )
    creds.refresh(Request())
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = service.channels().list(part="statistics", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        return _err(week_start, "YT OAuth: no channel for token")
    subs = items[0].get("statistics", {}).get("subscriberCount")
    return _ok(week_start, subs)


def _fetch_via_apikey(week_start: dt.date) -> ChannelSnapshot:
    resp = fetch_with_retry(
        YT_CHANNELS_API,
        method="GET",
        params={
            "part": "statistics",
            "id": os.environ["YT_CHANNEL_ID"],
            "key": os.environ["YT_API_KEY"],
        },
    )
    data = resp.json()
    items = data.get("items") or []
    if not items:
        return _err(week_start, "YT: channel not found")
    subs = items[0].get("statistics", {}).get("subscriberCount")
    return _ok(week_start, subs)


def fetch_weekly(week_start: dt.date) -> ChannelSnapshot:
    has_oauth = all(
        os.environ.get(k) for k in ("YT_REFRESH_TOKEN", "YT_CLIENT_ID", "YT_CLIENT_SECRET")
    )
    has_key = all(os.environ.get(k) for k in ("YT_API_KEY", "YT_CHANNEL_ID"))

    if has_oauth:
        try:
            snap = _fetch_via_oauth(week_start)
        except Exception as exc:  # noqa: BLE001
            snap = _err(week_start, f"YT OAuth {type(exc).__name__}: {str(exc)[:70]}")
        # Отдаём OAuth-результат, если он с числом ИЛИ ключа для fallback нет.
        if snap.subscribers is not None or not has_key:
            return snap
        # OAuth не смог (scope), но есть API-ключ → проваливаемся в путь 2.

    if has_key:
        try:
            return _fetch_via_apikey(week_start)
        except Exception as exc:  # noqa: BLE001
            return _err(week_start, f"YT apikey {type(exc).__name__}: {str(exc)[:70]}")

    return _err(week_start, "YT creds not set (optional)")
