from __future__ import annotations

import dataclasses

import pytest

from src.hybrid_report.models import (
    AppMetricaActivity,
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    PRODUCTS,
    ProductReport,
    ProductSpec,
    RegActivation,
    ScreenStat,
)


def test_products_has_exactly_centry_and_diktum():
    assert len(PRODUCTS) == 2
    keys = [p.key for p in PRODUCTS]
    assert keys == ["centry", "diktum"]
    assert {p.display for p in PRODUCTS} == {"Centry", "Diktum"}


def test_products_appmetrica_app_ids():
    by_key = {p.key: p for p in PRODUCTS}
    assert by_key["centry"].appmetrica_app_id == "6301660"
    assert by_key["diktum"].appmetrica_app_id == "6301663"


def test_centry_onboarding_first_and_last_steps():
    centry = next(p for p in PRODUCTS if p.key == "centry")
    assert centry.onboarding_steps[0][0] == "app_open"
    assert centry.onboarding_steps[-1][0] == "nickname_submitted"
    # каждая запись — пара (label, фраза)
    for label, phrase in centry.onboarding_steps:
        assert isinstance(label, str) and label
        assert isinstance(phrase, str) and phrase


def test_diktum_onboarding_first_and_last_steps():
    diktum = next(p for p in PRODUCTS if p.key == "diktum")
    assert diktum.onboarding_steps[0][0] == "app_open"
    assert diktum.onboarding_steps[-1][0] == "analysis_succeeded"


def test_dataclasses_are_frozen():
    spec = PRODUCTS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.key = "x"  # type: ignore[misc]

    act = AppMetricaActivity(sessions=1, active_users=1, avg_session_sec=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        act.sessions = 2  # type: ignore[misc]


def test_dataclasses_construct():
    step = FunnelStep(label="открыли", devices=25)
    assert step.devices == 25
    funnel = AppMetricaFunnel(steps=[step])
    assert funnel.error is None
    screen = ScreenStat(name="feed", views=40)
    assert screen.avg_sec is None
    screens = AppMetricaScreens(screens=[screen])
    assert screens.error is None
    reg = RegActivation(registrations=11, activations=8)
    assert reg.registrations == 11


def test_product_report_defaults():
    import datetime as dt

    spec = PRODUCTS[0]
    report = ProductReport(
        spec=spec,
        week_start=dt.date(2026, 5, 23),
        week_end=dt.date(2026, 5, 29),
    )
    assert report.store_snaps == []
    assert report.am_installs_total is None
    assert report.activity.sessions is None
    assert report.funnel.steps == []
    assert report.screens.screens == []
    assert report.reg.registrations is None
    assert report.prev_am_installs_total is None
    assert isinstance(report.spec, ProductSpec)
