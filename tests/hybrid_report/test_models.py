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


def test_products_has_all_six_studio_apps():
    assert len(PRODUCTS) == 6
    keys = [p.key for p in PRODUCTS]
    assert keys == ["centry", "diktum", "lucea", "lapulya", "unia", "listvia"]
    assert {p.display for p in PRODUCTS} == {
        "Centry", "Diktum", "Lucea", "Лапуля", "Unia", "Листвия"
    }


def test_products_appmetrica_app_ids():
    by_key = {p.key: p for p in PRODUCTS}
    assert by_key["centry"].appmetrica_app_id == "6301660"
    assert by_key["diktum"].appmetrica_app_id == "6301663"
    assert by_key["lucea"].appmetrica_app_id == "6303610"
    assert by_key["lapulya"].appmetrica_app_id == "6307939"
    assert by_key["unia"].appmetrica_app_id == "6308782"
    assert by_key["listvia"].appmetrica_app_id == "6316003"


def test_screen_event_label_per_product():
    by_key = {p.key: p for p in PRODUCTS}
    # Centry/Diktum шлют screen_view (строковое имя экрана в paramsLevel2)
    assert by_key["centry"].screen_event_label == "screen_view"
    assert by_key["diktum"].screen_event_label == "screen_view"
    # Новые приложения шлют screen_entered (int screen_id в paramsLevel2)
    for k in ("lucea", "lapulya", "unia", "listvia"):
        assert by_key[k].screen_event_label == "screen_entered"
        # ключи screen_names — строковые int screen_id
        assert by_key[k].screen_names["1"]


def test_new_apps_onboarding_starts_with_app_opened_first():
    by_key = {p.key: p for p in PRODUCTS}
    for k in ("lucea", "lapulya", "unia", "listvia"):
        steps = by_key[k].onboarding_steps
        assert steps[0][0] == "app_opened_first"
        assert len(steps) >= 5
        for label, phrase in steps:
            assert isinstance(label, str) and label
            assert isinstance(phrase, str) and phrase


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


def test_products_have_screen_names_maps():
    for p in PRODUCTS:
        assert isinstance(p.screen_names, dict)
        assert p.screen_names, f"{p.key} screen_names must be non-empty"
    by_key = {p.key: p for p in PRODUCTS}
    # Centry — raw имена экранов AppMetrica.
    assert by_key["centry"].screen_names["agreement"] == "соглашение"
    assert by_key["centry"].screen_names["activity_feed"] == "лента активности"
    # Diktum — ключи это go_router-пути.
    assert by_key["diktum"].screen_names["/auth"] == "вход"
    assert by_key["diktum"].screen_names["/analysis/:id"] == "результат анализа"


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
    assert report.am_installs_by_store == []
    assert report.am_store_error is None
    assert report.am_installs_total is None
    assert report.activity.sessions is None
    assert report.funnel.steps == []
    assert report.screens.screens == []
    assert report.reg.registrations is None
    assert report.prev_am_installs_total is None
    assert isinstance(report.spec, ProductSpec)
