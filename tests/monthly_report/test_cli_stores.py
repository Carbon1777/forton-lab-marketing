"""Тесты сбора store_snaps в monthly_report.cli (260611-8za).

Паттерн hybrid_report/gather: per-store try, функция модуля берётся в момент
вызова → patch.object(cli.<mod>, "fetch_monthly") работает.
"""
from __future__ import annotations

import datetime as dt
from contextlib import ExitStack
from unittest.mock import patch

from src.hybrid_report.models import (
    AppMetricaActivity,
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    PRODUCTS,
    ScreenStat,
)
from src.monthly_report import cli
from src.store_metrics.models import StoreSnapshot
from src.hybrid_report.appmetrica import InstallsBySource, InstallsByStore
from src.centry_funnel.supabase_src import FunnelDB as CFunnel

CENTRY = next(p for p in PRODUCTS if p.key == "centry")
M_START = dt.date(2026, 5, 1)
M_END = dt.date(2026, 5, 31)


def _snap(store: str, installs: int | None):
    return StoreSnapshot(product="centry", store=store, week_start=M_START,
                         installs=installs)


# ===================================================================
# _collect_stores
# ===================================================================

def test_collect_stores_all_ok():
    calls: list[tuple] = []

    def mk(store):
        def fake(product, year, month):
            calls.append((store, product, year, month))
            return _snap(store, 5)
        return fake

    with patch.object(cli.asc, "fetch_monthly", side_effect=mk("app_store")), \
         patch.object(cli.play, "fetch_monthly", side_effect=mk("google_play")), \
         patch.object(cli.rustore, "fetch_monthly", side_effect=mk("rustore")):
        snaps, store_error = cli._collect_stores("centry", M_START)

    assert store_error is None
    assert len(snaps) == 3
    assert [s.store for s in snaps] == ["app_store", "google_play", "rustore"]
    # Каждый модуль вызван с (product, year, month) от month_start.
    assert calls == [
        ("app_store", "centry", 2026, 5),
        ("google_play", "centry", 2026, 5),
        ("rustore", "centry", 2026, 5),
    ]


def test_collect_stores_one_failure_others_alive():
    with patch.object(cli.asc, "fetch_monthly",
                      side_effect=RuntimeError("ASC exploded")), \
         patch.object(cli.play, "fetch_monthly",
                      return_value=_snap("google_play", 7)), \
         patch.object(cli.rustore, "fetch_monthly",
                      return_value=_snap("rustore", None)):
        snaps, store_error = cli._collect_stores("centry", M_START)

    assert store_error is None
    assert len(snaps) == 3
    asc_snap = snaps[0]
    assert asc_snap.store == "app_store"
    assert asc_snap.installs is None
    assert asc_snap.error is not None
    assert "ASC exploded" in asc_snap.error
    assert asc_snap.week_start == M_START
    assert snaps[1].installs == 7


def test_collect_stores_all_fail_sets_store_error():
    boom = RuntimeError("down")
    with patch.object(cli.asc, "fetch_monthly", side_effect=boom), \
         patch.object(cli.play, "fetch_monthly", side_effect=boom), \
         patch.object(cli.rustore, "fetch_monthly", side_effect=boom):
        snaps, store_error = cli._collect_stores("centry", M_START)

    assert store_error == "all stores failed"
    assert len(snaps) == 3
    assert all(s.installs is None and s.error for s in snaps)


# ===================================================================
# _gather_product_monthly — прокидывание снапшотов в ProductReport
# ===================================================================

def _patch_all_sources():
    """Все источники успешны (стиль tests/hybrid_report/test_gather.py)."""
    return [
        patch.object(cli.asc, "fetch_monthly",
                     return_value=_snap("app_store", 25)),
        patch.object(cli.play, "fetch_monthly",
                     return_value=_snap("google_play", 12)),
        patch.object(cli.rustore, "fetch_monthly",
                     return_value=_snap("rustore", None)),
        patch.object(cli.appmetrica, "fetch_installs",
                     return_value=InstallsBySource(
                         total=40, organic=30, ads=10,
                         by_publisher={"Органика": 30, "VK Ads": 10})),
        patch.object(cli.centry_db, "fetch_funnel",
                     return_value=CFunnel(new_profiles=20, guests=9, users=11,
                                          activations=8)),
        patch.object(cli.appmetrica, "fetch_activity",
                     return_value=AppMetricaActivity(50, 25, 54.0)),
        patch.object(cli.appmetrica, "fetch_onboarding_funnel",
                     return_value=AppMetricaFunnel(
                         steps=[FunnelStep("открыли приложение", 25)])),
        patch.object(cli.appmetrica, "fetch_top_screens",
                     return_value=AppMetricaScreens(
                         screens=[ScreenStat("лента", 40)])),
        patch.object(cli.appmetrica, "fetch_installs_by_store",
                     return_value=InstallsByStore(
                         rows=[("App Store", 25), ("Google Play", 12),
                               ("RuStore", 3)], total=40)),
    ]


def test_gather_product_monthly_passes_store_snaps():
    with ExitStack() as stack:
        for cm in _patch_all_sources():
            stack.enter_context(cm)
        report = cli._gather_product_monthly(CENTRY, M_START, M_END, {})

    assert len(report.store_snaps) == 3
    assert [s.store for s in report.store_snaps] == [
        "app_store", "google_play", "rustore",
    ]
    assert report.store_snaps[0].installs == 25
    assert report.store_error is None
    # стор-блок витрины — из AppMetrica appInstaller
    assert report.am_installs_by_store == [
        ("App Store", 25), ("Google Play", 12), ("RuStore", 3)]
    assert report.am_store_error is None
    # Остальные источники не пострадали.
    assert report.am_installs_total == 40
