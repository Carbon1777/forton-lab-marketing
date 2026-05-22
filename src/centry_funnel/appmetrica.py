"""AppMetrica Reporting API — установки Centry по источникам.

Метрика ym:ts:installDevices, разбивка по ym:ts:publisher (Органика / реклама).
App id 6301660 (Centry). TZ приложения — Europe/Moscow. Токен общий
(APPMETRICA_OAUTH_TOKEN, scope appmetrica:read).
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from src.store_metrics._http import fetch_with_retry

APP_ID = "6301660"
STAT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"
ORGANIC_NAME = "Органика"


@dataclass(frozen=True)
class InstallsBySource:
    total: int
    organic: int
    ads: int
    by_publisher: dict[str, int]


def _token() -> str:
    t = os.environ.get("APPMETRICA_OAUTH_TOKEN")
    if not t:
        raise RuntimeError("APPMETRICA_OAUTH_TOKEN missing")
    return t


def fetch_installs(
    week_start: dt.date, week_end: dt.date, token: str | None = None
) -> InstallsBySource:
    """Установки за период [week_start, week_end] с разбивкой по источнику."""
    token = token or _token()
    resp = fetch_with_retry(
        STAT_URL,
        method="GET",
        headers={"Authorization": f"OAuth {token}"},
        params={
            "id": APP_ID,
            "date1": week_start.isoformat(),
            "date2": week_end.isoformat(),
            "metrics": "ym:ts:installDevices",
            "dimensions": "ym:ts:publisher",
            "accuracy": "full",
            "lang": "ru",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    by_publisher: dict[str, int] = {}
    for row in payload.get("data", []):
        name = row["dimensions"][0]["name"]
        value = int(row["metrics"][0] or 0)
        by_publisher[name] = value
    total = sum(by_publisher.values())
    organic = by_publisher.get(ORGANIC_NAME, 0)
    ads = total - organic
    return InstallsBySource(
        total=total, organic=organic, ads=ads, by_publisher=by_publisher
    )
