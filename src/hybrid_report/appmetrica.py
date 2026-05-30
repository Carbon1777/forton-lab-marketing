"""AppMetrica Reporting API — НОВЫЕ метрики для гибридного отчёта.

Активность (ym:s:*): sessions / users / avgSessionDuration.

Installs-источник (ym:ts:*) НЕ здесь — он переиспользуется импортом из
*_funnel.appmetrica (gather, Task 5).

Грабли (verified живым вызовом 2026-05-30):
  - НЕ мешать префиксы ym:ts:* и ym:s:* в одном запросе → ошибка 4011.
  - accuracy=full обязателен. TZ приложения Europe/Moscow.
"""
from __future__ import annotations

import datetime as dt
import os

from src.store_metrics._http import fetch_with_retry
from .models import AppMetricaActivity

STAT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"


def _token(token: str | None) -> str:
    t = token or os.environ.get("APPMETRICA_OAUTH_TOKEN")
    if not t:
        raise RuntimeError("APPMETRICA_OAUTH_TOKEN missing")
    return t


def _totals(payload: dict) -> list:
    return payload.get("totals") or []


def fetch_activity(
    app_id: str,
    week_start: dt.date,
    week_end: dt.date,
    token: str | None = None,
) -> AppMetricaActivity:
    """Активность за период: ym:s:sessions / ym:s:users / ym:s:avgSessionDuration.

    Агрегат за период (БЕЗ dimensions). Пустой ответ → нули (не падает).
    Чистый ym:s: namespace — НЕ добавлять ym:ts:* метрики (ошибка 4011).
    """
    tok = _token(token)
    resp = fetch_with_retry(
        STAT_URL,
        method="GET",
        headers={"Authorization": f"OAuth {tok}"},
        params={
            "id": app_id,
            "date1": week_start.isoformat(),
            "date2": week_end.isoformat(),
            "metrics": "ym:s:sessions,ym:s:users,ym:s:avgSessionDuration",
            "accuracy": "full",
            "lang": "ru",
        },
    )
    resp.raise_for_status()
    totals = _totals(resp.json())
    sessions = int(totals[0]) if len(totals) > 0 else 0
    active_users = int(totals[1]) if len(totals) > 1 else 0
    avg_session_sec = float(totals[2]) if len(totals) > 2 else 0.0
    return AppMetricaActivity(
        sessions=sessions,
        active_users=active_users,
        avg_session_sec=avg_session_sec,
    )
