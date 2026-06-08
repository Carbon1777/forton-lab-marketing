from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src.hybrid_report import gather
from src.hybrid_report.models import (
    AppMetricaActivity,
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    PRODUCTS,
    RegActivation,
    ScreenStat,
)
from src.store_metrics.models import StoreSnapshot
from src.hybrid_report.appmetrica import InstallsBySource
from src.centry_funnel.supabase_src import FunnelDB as CFunnel
from src.diktum_funnel.supabase_src import FunnelDB as DFunnel

CENTRY = next(p for p in PRODUCTS if p.key == "centry")
DIKTUM = next(p for p in PRODUCTS if p.key == "diktum")
W_START = dt.date(2026, 5, 23)
W_END = dt.date(2026, 5, 29)


def _snap(store: str, installs: int | None):
    return StoreSnapshot(product="centry", store=store, week_start=W_START,
                         installs=installs)


def _patch_all_success():
    """Контекст-менеджеры на все источники, успешный путь."""
    return [
        patch.object(gather.asc, "fetch_weekly",
                     return_value=_snap("app_store", 5)),
        patch.object(gather.play, "fetch_weekly",
                     return_value=_snap("google_play", 12)),
        patch.object(gather.rustore, "fetch_weekly",
                     return_value=_snap("rustore", None)),
        patch.object(gather.appmetrica, "fetch_installs",
                     return_value=InstallsBySource(
                         total=24, organic=18, ads=6,
                         by_publisher={"Органика": 18, "VK Ads": 6})),
        patch.object(gather.centry_db, "fetch_funnel",
                     return_value=CFunnel(new_profiles=20, guests=9, users=11,
                                          activations=8)),
        patch.object(gather.appmetrica, "fetch_activity",
                     return_value=AppMetricaActivity(50, 25, 54.0)),
        patch.object(gather.appmetrica, "fetch_onboarding_funnel",
                     return_value=AppMetricaFunnel(
                         steps=[FunnelStep("открыли приложение", 25)])),
        patch.object(gather.appmetrica, "fetch_top_screens",
                     return_value=AppMetricaScreens(
                         screens=[ScreenStat("лента", 40)])),
    ]


def test_gather_assembles_all_sources():
    from contextlib import ExitStack
    with ExitStack() as stack:
        for cm in _patch_all_success():
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, {})
    assert report.am_installs_total == 24
    assert report.am_installs_organic == 18
    assert report.am_installs_ads == 6
    assert report.am_ads_publisher == "VK Ads"
    assert report.am_installs_error is None
    assert report.activity == AppMetricaActivity(50, 25, 54.0)
    assert report.funnel.steps == [FunnelStep("открыли приложение", 25)]
    assert report.screens.screens == [ScreenStat("лента", 40)]
    # Centry semantics: registrations = users (11), activations = activations (8)
    assert report.reg == RegActivation(registrations=11, activations=8)
    assert len(report.store_snaps) == 3
    assert report.store_error is None


def test_gather_centry_reg_maps_users_not_new_profiles():
    from contextlib import ExitStack
    with ExitStack() as stack:
        for cm in _patch_all_success():
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, {})
    # new_profiles=20 НЕ должно попасть в registrations; users=11 должно
    assert report.reg.registrations == 11
    assert report.reg.registrations != 20


def test_gather_diktum_reg_maps_directly():
    from contextlib import ExitStack
    cms = [
        patch.object(gather.asc, "fetch_weekly", return_value=_snap("app_store", 1)),
        patch.object(gather.play, "fetch_weekly", return_value=_snap("google_play", 2)),
        patch.object(gather.rustore, "fetch_weekly", return_value=_snap("rustore", None)),
        patch.object(gather.appmetrica, "fetch_installs",
                     return_value=InstallsBySource(total=121, organic=100, ads=21,
                                                   by_publisher={"Органика": 100, "VK Ads": 21})),
        patch.object(gather.diktum_db, "fetch_registrations",
                     return_value=DFunnel(registrations=30, activated=12)),
        patch.object(gather.appmetrica, "fetch_activity",
                     return_value=AppMetricaActivity(121, 53, 60.0)),
        patch.object(gather.appmetrica, "fetch_onboarding_funnel",
                     return_value=AppMetricaFunnel(steps=[])),
        patch.object(gather.appmetrica, "fetch_top_screens",
                     return_value=AppMetricaScreens(screens=[])),
    ]
    with ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        report = gather.gather_product(DIKTUM, W_START, W_END, {})
    assert report.reg == RegActivation(registrations=30, activations=12)
    assert report.am_installs_total == 121


def test_gather_never_raises_on_store_failure():
    from contextlib import ExitStack
    cms = _patch_all_success()
    cms[0] = patch.object(gather.asc, "fetch_weekly",
                          side_effect=RuntimeError("asc down"))
    with ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, {})
    # не падает; app_store снап с error, остальные блоки заполнены
    assert len(report.store_snaps) == 3
    app_snap = next(s for s in report.store_snaps if s.store == "app_store")
    assert app_snap.installs is None
    assert app_snap.error is not None
    assert report.am_installs_total == 24  # остальное собрано


def test_gather_never_raises_on_appmetrica_activity_failure():
    from contextlib import ExitStack
    cms = _patch_all_success()
    cms[5] = patch.object(gather.appmetrica, "fetch_activity",
                          side_effect=RuntimeError("am down"))
    with ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, {})
    assert report.activity == AppMetricaActivity(None, None, None)
    assert report.am_installs_total == 24  # остальное собрано


def test_gather_never_raises_on_installs_failure():
    from contextlib import ExitStack
    cms = _patch_all_success()
    cms[3] = patch.object(gather.appmetrica, "fetch_installs",
                          side_effect=RuntimeError("installs down"))
    with ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, {})
    assert report.am_installs_total is None
    assert report.am_installs_error is not None
    assert report.activity.sessions == 50  # остальное собрано


def test_gather_never_raises_on_supabase_failure():
    from contextlib import ExitStack
    cms = _patch_all_success()
    cms[4] = patch.object(gather.centry_db, "fetch_funnel",
                          side_effect=RuntimeError("db down"))
    with ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, {})
    assert report.reg == RegActivation(None, None)
    assert report.am_installs_total == 24  # остальное собрано


def test_gather_reads_prev_installs():
    from contextlib import ExitStack
    # снапшот с прошлой неделей (W_START - 7 = 2026-05-16 → W20)
    snapshots = {}
    from src.hybrid_report import snapshot
    snapshot.store_week(snapshots, W_START - dt.timedelta(days=7), "centry", 20)
    with ExitStack() as stack:
        for cm in _patch_all_success():
            stack.enter_context(cm)
        report = gather.gather_product(CENTRY, W_START, W_END, snapshots)
    assert report.prev_am_installs_total == 20
