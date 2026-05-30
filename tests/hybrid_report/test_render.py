from __future__ import annotations

import datetime as dt

from src.hybrid_report.models import (
    AppMetricaActivity,
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    PRODUCTS,
    ProductReport,
    RegActivation,
    ScreenStat,
)
from src.hybrid_report.render import render_report
from src.store_metrics.models import StoreSnapshot

CENTRY = next(p for p in PRODUCTS if p.key == "centry")
W_START = dt.date(2026, 5, 23)
W_END = dt.date(2026, 5, 29)

# Запрещённый набор символов (HARD requirement формата).
FORBIDDEN = "→←↑↓⬆⬇📈📉📊•·└├▸%⭐🎯🚨💡🌍⚠"


def _stores() -> list[StoreSnapshot]:
    return [
        StoreSnapshot(product="centry", store="app_store", week_start=W_START,
                      installs=5, rating=4.5),
        StoreSnapshot(product="centry", store="google_play", week_start=W_START,
                      installs=12),
        StoreSnapshot(product="centry", store="rustore", week_start=W_START,
                      installs=None),
    ]


def _report(**over) -> ProductReport:
    base = dict(
        spec=CENTRY,
        week_start=W_START,
        week_end=W_END,
        store_snaps=_stores(),
        am_installs_total=24,
        am_installs_organic=18,
        am_installs_ads=6,
        am_ads_publisher="VK Ads",
        activity=AppMetricaActivity(sessions=50, active_users=25,
                                    avg_session_sec=54.0),
        funnel=AppMetricaFunnel(steps=[
            FunnelStep("открыли приложение", 25),
            FunnelStep("посмотрели интро", 22),
            FunnelStep("отправили email", 20),
            FunnelStep("завершили регистрацию", 11),
        ]),
        screens=AppMetricaScreens(screens=[
            ScreenStat("лента", 40), ScreenStat("план", 22),
            ScreenStat("профиль", 15),
        ]),
        reg=RegActivation(registrations=11, activations=8),
        prev_am_installs_total=20,
    )
    base.update(over)
    return ProductReport(**base)


def test_render_has_no_emoji_or_arrows():
    text = render_report(_report())
    assert not any(c in text for c in FORBIDDEN), (
        f"forbidden chars present: {[c for c in FORBIDDEN if c in text]}"
    )
    assert "://" not in text  # нет ссылок


def test_render_stores_block():
    text = render_report(_report())
    assert "App Store — 5" in text
    assert "Google Play — 12" in text
    assert "RuStore — нет данных" in text
    assert "Всего 17" in text


def test_render_appmetrica_installs_block():
    text = render_report(_report())
    assert "Установки по данным AppMetrica: 24" in text
    assert "18 (75 процентов)" in text
    assert "VK Ads" in text
    assert "6 (25 процентов)" in text
    assert "не складываются" in text


def test_render_activity_block():
    text = render_report(_report())
    assert "50 сессий" in text
    assert "25 активных пользователей" in text
    assert "средняя сессия 54 секунды" in text


def test_render_funnel_block_with_max_dropoff():
    text = render_report(_report())
    # каждый шаг присутствует с числом
    assert "открыли приложение — 25" in text
    assert "посмотрели интро — 22" in text
    assert "отправили email — 20" in text
    assert "завершили регистрацию — 11" in text
    # конверсия словом
    assert "процентов" in text
    # шаг макс. отвала: 20 -> 11 = -45% (max), назван явно
    assert "Наибольший отвал" in text
    assert "завершили регистрацию" in text


def test_render_screens_block():
    text = render_report(_report())
    assert "Экраны" in text
    assert "лента — 40" in text
    assert "план — 22" in text


def test_render_reg_activation_block():
    text = render_report(_report())
    assert "зарегистрировались 11, активировались 8 (73 процента)" in text


def test_render_retention_stub():
    text = render_report(_report())
    assert "Удержание" in text
    assert "пока недоступно" in text


def test_render_wow_more():
    text = render_report(_report(prev_am_installs_total=20, am_installs_total=24))
    assert "больше на 20 процентов (было 20)" in text


def test_render_wow_less():
    text = render_report(_report(prev_am_installs_total=30, am_installs_total=24))
    assert "меньше на 20 процентов (было 30)" in text


def test_render_wow_same():
    text = render_report(_report(prev_am_installs_total=24, am_installs_total=24))
    assert "столько же" in text


def test_render_wow_no_prev():
    text = render_report(_report(prev_am_installs_total=None))
    assert "прошлая неделя недоступна" in text


def test_render_first_line_title():
    text = render_report(_report())
    first_line = text.splitlines()[0]
    assert first_line == "Centry — отчёт за неделю 23–29 мая"


def test_render_degraded_funnel():
    text = render_report(_report(
        funnel=AppMetricaFunnel(steps=[], error="RuntimeError: boom")
    ))
    assert "данные собираются" in text
    # не падает, остальные блоки есть
    assert "App Store — 5" in text


def test_render_degraded_screens():
    text = render_report(_report(
        screens=AppMetricaScreens(screens=[], error="ValueError: x")
    ))
    assert "данные собираются" in text


def test_render_degraded_stores_all_none():
    snaps = [
        StoreSnapshot(product="centry", store="app_store", week_start=W_START,
                      installs=None),
        StoreSnapshot(product="centry", store="google_play", week_start=W_START,
                      installs=None),
        StoreSnapshot(product="centry", store="rustore", week_start=W_START,
                      installs=None),
    ]
    text = render_report(_report(store_snaps=snaps))
    assert "App Store — нет данных" in text
    assert "Google Play — нет данных" in text
    assert "RuStore — нет данных" in text


def test_render_degraded_am_installs_none():
    text = render_report(_report(
        am_installs_total=None, am_installs_organic=None, am_installs_ads=None,
        am_ads_publisher=None,
    ))
    assert "данные собираются" in text


def test_render_degraded_activity_none():
    text = render_report(_report(
        activity=AppMetricaActivity(None, None, None)
    ))
    # активность деградирует, но сообщение не падает
    assert "App Store — 5" in text
