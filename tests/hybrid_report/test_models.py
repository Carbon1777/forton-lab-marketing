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


def test_products_has_active_studio_apps():
    assert len(PRODUCTS) == 4
    keys = [p.key for p in PRODUCTS]
    assert keys == ["diktum", "lapulya", "listvia", "lucea"]
    assert {p.display for p in PRODUCTS} == {"Diktum", "Лапуля", "Листвия", "Lucea"}


def test_products_exclude_closed_apps():
    """Centry/Unia закрыты и сняты со сторов (2026-10-01) — в отчёте их быть не
    должно (иначе шлём мусор по мёртвым приложениям). Lucea вернулась (2.0)."""
    keys = {p.key for p in PRODUCTS}
    assert keys.isdisjoint({"centry", "unia"})


def test_products_appmetrica_app_ids():
    by_key = {p.key: p for p in PRODUCTS}
    assert by_key["diktum"].appmetrica_app_id == "6301663"
    assert by_key["lapulya"].appmetrica_app_id == "6307939"
    assert by_key["listvia"].appmetrica_app_id == "6316003"
    assert by_key["lucea"].appmetrica_app_id == "6375118"


def test_screen_event_label_per_product():
    by_key = {p.key: p for p in PRODUCTS}
    # Diktum шлёт screen_view (строковое имя экрана в paramsLevel2)
    assert by_key["diktum"].screen_event_label == "screen_view"
    # Новые приложения шлют screen_entered (int screen_id в paramsLevel2)
    for k in ("lapulya", "listvia"):
        assert by_key[k].screen_event_label == "screen_entered"
        # ключи screen_names — строковые int screen_id
        assert by_key[k].screen_names["1"]


def test_new_apps_onboarding_starts_with_app_opened_first():
    by_key = {p.key: p for p in PRODUCTS}
    for k in ("lapulya", "listvia"):
        steps = by_key[k].onboarding_steps
        assert steps[0][0] == "app_opened_first"
        assert len(steps) >= 5
        for label, phrase in steps:
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


def test_lapulya_v2_funnels_and_screens():
    import re
    from src.hybrid_report.models import FunnelSpec, FunnelStepSpec

    lap = next(p for p in PRODUCTS if p.key == "lapulya")
    assert [f.name for f in lap.extra_funnels] == [
        "Облако", "Вход", "Pro", "Семья", "Дежурства",
        "Отчёт и расходы", "ИИ",
    ]
    assert lap.review_event == "review_prompt_triggered"
    assert {str(i) for i in range(1, 35)} <= set(lap.screen_names)
    assert lap.screen_names["24"] == "вход"
    assert lap.screen_names["34"] == "удаление аккаунта"
    hash(lap.extra_funnels)  # хешируемость (frozen + tuple)
    for f in lap.extra_funnels:
        assert isinstance(f, FunnelSpec)
        for s in f.steps:
            assert isinstance(s, FunnelStepSpec)
            assert re.fullmatch(r"[a-z0-9_]+", s.event)
            assert s.phrase
            if s.param is not None:
                k, v = s.param
                assert isinstance(k, str) and k and isinstance(v, str) and v
    reg = lap.extra_funnels[0].steps[3]
    assert (reg.event, reg.param) == ("account_signin_ok", ("method", "2"))


def test_other_products_have_no_extra_funnels():
    for p in PRODUCTS:
        if p.key != "lapulya":
            assert p.extra_funnels == ()
