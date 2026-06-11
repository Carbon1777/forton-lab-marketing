"""Тесты рендера стор-блока месячного отчёта (260611-8za).

Word-based стиль: без эмодзи, без знака %. RuStore без числа → честная
фраза про ограничение Mail.ru. Итог «(без RuStore)» — только когда RuStore
без числа.
"""
from __future__ import annotations

import datetime as dt

from src.hybrid_report.models import PRODUCTS, ProductReport
from src.monthly_report.render import _block_stores, render_monthly_report
from src.store_metrics.models import StoreSnapshot

CENTRY = next(p for p in PRODUCTS if p.key == "centry")
M_START = dt.date(2026, 5, 1)
M_END = dt.date(2026, 5, 31)


def _snap(store: str, installs: int | None, rating: float | None = None):
    return StoreSnapshot(
        product="centry", store=store, week_start=M_START,
        installs=installs, rating=rating,
    )


def _report(store_snaps: list[StoreSnapshot]) -> ProductReport:
    return ProductReport(
        spec=CENTRY, week_start=M_START, week_end=M_END,
        store_snaps=store_snaps, store_error=None,
    )


def test_block_stores_full_set_renders_numbers_and_rustore_phrase():
    r = _report([
        _snap("app_store", 25, rating=4.7),
        _snap("google_play", 12, rating=4.5),
        _snap("rustore", None, rating=4.8),
    ])
    line = _block_stores(r)
    assert "App Store — 25" in line
    assert "Google Play — 12" in line
    assert "Всего за месяц 37 (без RuStore)." in line
    assert "RuStore не отдаёт установки через API (ограничение Mail.ru)" in line
    assert "Рейтинг: App Store 4.7, Google Play 4.5, RuStore 4.8." in line


def test_block_stores_empty_snaps_degrades():
    r = _report([])
    assert _block_stores(r) == "Скачивания по сторам: данные собираются."


def test_block_stores_all_installs_none_no_total():
    r = _report([
        _snap("app_store", None),
        _snap("google_play", None),
        _snap("rustore", None),
    ])
    line = _block_stores(r)
    assert "App Store — нет данных" in line
    assert "Google Play — нет данных" in line
    assert "RuStore не отдаёт установки через API" in line
    assert "Всего" not in line
    assert "Рейтинг" not in line


def test_block_stores_rustore_with_number_renders_number():
    """Mock-режим / будущий API: RuStore с числом → число, без суффикса."""
    r = _report([
        _snap("app_store", 10),
        _snap("google_play", 5),
        _snap("rustore", 3),
    ])
    line = _block_stores(r)
    assert "RuStore — 3" in line
    assert "Всего за месяц 18." in line
    assert "(без RuStore)" not in line
    assert "не отдаёт установки" not in line


def test_block_stores_no_emoji_no_percent_sign():
    r = _report([
        _snap("app_store", 25, rating=4.7),
        _snap("google_play", 12, rating=4.5),
        _snap("rustore", None),
    ])
    line = _block_stores(r)
    assert "%" not in line
    assert all(ord(ch) < 0x2190 for ch in line), "эмодзи/стрелки запрещены"


def test_full_report_contains_real_store_block_not_stub():
    r = _report([
        _snap("app_store", 25),
        _snap("google_play", 12),
        _snap("rustore", None),
    ])
    text = render_monthly_report(r)
    assert "доступна в недельных отчётах" not in text
    assert "Всего за месяц 37 (без RuStore)." in text
