"""Тесты рендера стор-блока месячного отчёта.

Источник стор-блока — AppMetrica appInstaller (как в недельном отчёте), в
разрезе месяца. Word-based стиль: без эмодзи, без знака %. RuStore теперь
включён (AppMetrica знает installer). store_snaps используются только для
рейтингов (опциональная сверка).
"""
from __future__ import annotations

import datetime as dt

from src.hybrid_report.models import PRODUCTS, ProductReport
from src.monthly_report.render import _block_stores, render_monthly_report
from src.store_metrics.models import StoreSnapshot

CENTRY = next(p for p in PRODUCTS if p.key == "centry")
M_START = dt.date(2026, 5, 1)
M_END = dt.date(2026, 5, 31)


def _snap(store: str, rating: float | None = None):
    return StoreSnapshot(
        product="centry", store=store, week_start=M_START,
        installs=None, rating=rating,
    )


def _report(
    am_rows: list[tuple[str, int]],
    store_snaps: list[StoreSnapshot] | None = None,
) -> ProductReport:
    return ProductReport(
        spec=CENTRY, week_start=M_START, week_end=M_END,
        am_installs_by_store=am_rows,
        store_snaps=store_snaps or [],
    )


def test_block_stores_renders_numbers_and_total():
    r = _report(
        [("App Store", 25), ("Google Play", 12), ("RuStore", 7)],
        store_snaps=[
            _snap("app_store", rating=4.7),
            _snap("google_play", rating=4.5),
            _snap("rustore", rating=4.8),
        ],
    )
    line = _block_stores(r)
    assert "Установки по магазинам: App Store — 25, Google Play — 12, RuStore — 7." in line
    assert "Всего за месяц 44." in line
    # рейтинги — из store_snaps (опциональная сверка)
    assert "Рейтинг: App Store 4.7, Google Play 4.5, RuStore 4.8." in line


def test_block_stores_order_and_unknown():
    rows = [("App Store", 47), ("Google Play", 19), ("RuStore", 3),
            ("Galaxy Store", 3), ("Android (источник неизвестен)", 8)]
    line = _block_stores(_report(rows))
    assert (
        "Установки по магазинам: App Store — 47, Google Play — 19, "
        "RuStore — 3, Galaxy Store — 3, "
        "Android (источник неизвестен) — 8. Всего за месяц 80." in line
    )


def test_block_stores_empty_degrades():
    assert _block_stores(_report([])) == "Установки по магазинам: данные собираются."


def test_block_stores_no_emoji_no_percent_sign():
    r = _report(
        [("App Store", 25), ("Google Play", 12)],
        store_snaps=[_snap("app_store", rating=4.7)],
    )
    line = _block_stores(r)
    assert "%" not in line
    assert all(ord(ch) < 0x2190 for ch in line), "эмодзи/стрелки запрещены"


def test_full_report_contains_real_store_block_not_stub():
    text = render_monthly_report(
        _report([("App Store", 25), ("Google Play", 12)])
    )
    assert "данные собираются" not in text.splitlines()[1]  # стор-строка не заглушка
    assert "Установки по магазинам: App Store — 25, Google Play — 12." in text
    assert "Всего за месяц 37." in text
