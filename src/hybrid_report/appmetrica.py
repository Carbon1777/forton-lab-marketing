"""AppMetrica Reporting API — НОВЫЕ метрики для гибридного отчёта.

Активность (ym:s:*): sessions / users / avgSessionDuration.
Воронка онбординга + топ-экраны (ym:ce:devices с фильтром ym:ce:eventLabel).

Installs-источник (ym:ts:*) НЕ здесь — он переиспользуется импортом из
*_funnel.appmetrica (gather, Task 5).

Грабли (verified живым вызовом 2026-05-30):
  - НЕ мешать префиксы ym:ts:* и ym:s:* в одном запросе → ошибка 4011.
  - Воронка/экраны: метрика именно ym:ce:devices, НЕ ym:ce:events (→ 4002).
  - Фильтр-синтаксис — двойной знак равно: ym:ce:eventLabel=='screen_view'.
  - accuracy=full обязателен. TZ приложения Europe/Moscow.
"""
from __future__ import annotations

import datetime as dt
import os

from src.store_metrics._http import fetch_with_retry
from .models import (
    AppMetricaActivity,
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    ScreenStat,
)

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


def fetch_onboarding_funnel(
    app_id: str,
    steps: list[tuple[str, str]],
    week_start: dt.date,
    week_end: dt.date,
    token: str | None = None,
) -> AppMetricaFunnel:
    """Воронка онбординга: per-step запрос ym:ce:devices, фильтр по eventLabel.

    Шаги с нулём ОСТАВЛЯЕМ (нужно показать падение до нуля; «шаг макс. отвала»
    считается в render). ЛЮБАЯ ошибка на любом шаге → мягкая деградация:
    AppMetricaFunnel(steps=[], error=...). Diktum ym:ce:devices живьём не
    проверен — деградация здесь критична.
    """
    try:
        tok = _token(token)
        result: list[FunnelStep] = []
        for label, phrase in steps:
            resp = fetch_with_retry(
                STAT_URL,
                method="GET",
                headers={"Authorization": f"OAuth {tok}"},
                params={
                    "id": app_id,
                    "date1": week_start.isoformat(),
                    "date2": week_end.isoformat(),
                    "metrics": "ym:ce:devices",
                    "filters": f"ym:ce:eventLabel=='{label}'",
                    "accuracy": "full",
                    "lang": "ru",
                },
            )
            resp.raise_for_status()
            totals = _totals(resp.json())
            devices = int(totals[0]) if totals else 0
            result.append(FunnelStep(label=phrase, devices=devices))
        return AppMetricaFunnel(steps=result, error=None)
    except Exception as exc:  # noqa: BLE001 — мягкая деградация секции
        return AppMetricaFunnel(
            steps=[], error=f"{type(exc).__name__}: {str(exc)[:80]}"
        )


def fetch_top_screens(
    app_id: str,
    week_start: dt.date,
    week_end: dt.date,
    token: str | None = None,
    top_n: int = 7,
) -> AppMetricaScreens:
    """Топ-N экранов по заходам: ym:ce:devices, dim ym:ce:paramsLevel2,
    фильтр eventLabel=='screen_view'. Сортировка по views убыв., срез [:top_n].

    paramsLevel1 = КЛЮЧ параметра ("screen"); имя экрана лежит в paramsLevel2
    (verified живым вызовом 25-31.05: Centry agreement/permissions/auth/...,
    Diktum /auth, register, /analysis/:id, ...). Поэтому dim = paramsLevel2.

    Ср. время на экране НЕ достаём в этой итерации (avg_sec=None) — допустимо
    по формату «+ ср. время если достанется». Ошибка → мягкая деградация.
    """
    try:
        tok = _token(token)
        resp = fetch_with_retry(
            STAT_URL,
            method="GET",
            headers={"Authorization": f"OAuth {tok}"},
            params={
                "id": app_id,
                "date1": week_start.isoformat(),
                "date2": week_end.isoformat(),
                "metrics": "ym:ce:devices",
                "dimensions": "ym:ce:paramsLevel2",
                "filters": "ym:ce:eventLabel=='screen_view'",
                "accuracy": "full",
                "lang": "ru",
            },
        )
        resp.raise_for_status()
        screens: list[ScreenStat] = []
        for row in resp.json().get("data", []):
            name = row["dimensions"][0]["name"]
            views = int(row["metrics"][0] or 0)
            screens.append(ScreenStat(name=name, views=views, avg_sec=None))
        screens.sort(key=lambda s: s.views, reverse=True)
        return AppMetricaScreens(screens=screens[:top_n], error=None)
    except Exception as exc:  # noqa: BLE001 — мягкая деградация секции
        return AppMetricaScreens(
            screens=[], error=f"{type(exc).__name__}: {str(exc)[:80]}"
        )
