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
from dataclasses import dataclass

from src.store_metrics._http import fetch_with_retry
from .models import (
    AppMetricaActivity,
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    ScreenStat,
)

STAT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"
ORGANIC_NAME = "Органика"


@dataclass(frozen=True)
class InstallsBySource:
    """Установки за период с разбивкой по источнику (publisher)."""
    total: int
    organic: int
    ads: int
    by_publisher: dict[str, int]


@dataclass(frozen=True)
class InstallsByStore:
    """Установки за период с разбивкой по магазину-установщику (appInstaller).

    rows — упорядоченный список (человекочитаемый_магазин, число_устройств).
    Порядок: основные сторы по приоритету, затем прочее по убыванию.
    """
    rows: list[tuple[str, int]]
    total: int


# Стабильный appInstaller id (НЕ зависит от lang ответа) → витринное имя магазина.
# id "android" = Android-установка, у которой installer не определился
# (sideload / неизвестный стор), а НЕ «все Android». iOS-установка всегда из
# App Store (других магазинов на iOS нет).
_UNKNOWN_ANDROID = "Android (источник неизвестен)"

_STORE_LABEL_BY_INSTALLER: dict[str, str] = {
    "ios": "App Store",
    "com.android.vending": "Google Play",
    "ru.vk.store": "RuStore",
    "com.sec.android.app.samsungapps": "Galaxy Store",
    "com.huawei.appmarket": "AppGallery",
    "com.amazon.venezia": "Amazon Appstore",
    # sideload / системный установщик / песочницы-клоны — не магазины; сводим в
    # один бакет «источник неизвестен», чтобы не сорить в отчёте техническими id.
    "android": _UNKNOWN_ANDROID,
    "com.google.android.packageinstaller": _UNKNOWN_ANDROID,
    "com.android.packageinstaller": _UNKNOWN_ANDROID,
    "com.miui.packageinstaller": _UNKNOWN_ANDROID,
    "com.gbox.android": _UNKNOWN_ANDROID,
}

# Приоритет вывода основных магазинов (меньше = выше). Остальное — после, по
# убыванию установок. Гарантирует стабильный порядок строк отчёта.
_STORE_ORDER_PRIORITY: dict[str, int] = {
    "App Store": 0,
    "Google Play": 1,
    "RuStore": 2,
    "Galaxy Store": 3,
    "AppGallery": 4,
    "Amazon Appstore": 5,
    _UNKNOWN_ANDROID: 90,
}


def _token(token: str | None) -> str:
    t = token or os.environ.get("APPMETRICA_OAUTH_TOKEN")
    if not t:
        raise RuntimeError("APPMETRICA_OAUTH_TOKEN missing")
    return t


def _totals(payload: dict) -> list:
    return payload.get("totals") or []


def fetch_installs(
    app_id: str,
    week_start: dt.date,
    week_end: dt.date,
    token: str | None = None,
) -> InstallsBySource:
    """Установки за период [week_start, week_end] с разбивкой по источнику.

    Метрика ym:ts:installDevices, разбивка ym:ts:publisher (Органика / реклама).
    Generic по app_id — один путь для всех 5 продуктов. Чистый ym:ts: namespace
    (НЕ смешивать с ym:s:* / ym:ce:* — ошибка 4011). Без рекламных кампаний всё
    падает в «Органика» (ads=0), что норма для новых приложений.
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


def fetch_installs_by_store(
    app_id: str,
    week_start: dt.date,
    week_end: dt.date,
    token: str | None = None,
) -> InstallsByStore:
    """Установки за период с разбивкой по магазину (надёжный источник стор-блока).

    Метрика ym:ts:installDevices, разбивка ym:ts:appInstaller. Заменяет хрупкие
    прямые стор-API (ASC / Google Play / RuStore), которые молча отдают ноль/«нет
    данных» при сбое токена/доступа. AppMetrica SDK пишет installer на каждой
    установке, поэтому iOS→App Store, com.android.vending→Google Play,
    ru.vk.store→RuStore и т.д. Маппинг по СТАБИЛЬНОМУ id (не по name — оно зависит
    от lang). Чистый ym:ts: namespace (НЕ мешать с ym:s:* / ym:ce:* — ошибка 4011).

    Verified живым вызовом 2026-06-24 (Diktum 6301663, 15–21 июня): id-значения
    ios / com.android.vending / android / com.sec.android.app.samsungapps /
    ru.vk.store; total сходится с ym:ts:publisher и веб-UI AppMetrica.
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
            "metrics": "ym:ts:installDevices",
            "dimensions": "ym:ts:appInstaller",
            "accuracy": "full",
            "lang": "ru",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    by_label: dict[str, int] = {}
    for row in payload.get("data", []):
        dim = row["dimensions"][0]
        installer_id = dim.get("id")
        # маппим по стабильному id; незнакомый installer — показываем его name
        label = _STORE_LABEL_BY_INSTALLER.get(
            installer_id, dim.get("name") or installer_id or "неизвестно"
        )
        value = int(row["metrics"][0] or 0)
        by_label[label] = by_label.get(label, 0) + value
    # порядок: приоритет основных сторов, затем прочее по убыванию установок
    rows = sorted(
        by_label.items(),
        key=lambda kv: (_STORE_ORDER_PRIORITY.get(kv[0], 50), -kv[1]),
    )
    total = sum(by_label.values())
    return InstallsByStore(rows=rows, total=total)


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
    event_label: str = "screen_view",
    token: str | None = None,
    top_n: int = 7,
) -> AppMetricaScreens:
    """Топ-N экранов по заходам: ym:ce:devices, dim ym:ce:paramsLevel2,
    фильтр eventLabel==event_label. Сортировка по views убыв., срез [:top_n].

    event_label: "screen_view" (Centry/Diktum — paramsLevel2 = строковое имя
    экрана) либо "screen_entered" (Lucea/Unia/Лапуля — paramsLevel2 = int
    screen_id, который render маппит в screen_names).

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
                "filters": f"ym:ce:eventLabel=='{event_label}'",
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
