"""In-App Review блок: рендер 4 состояний + гейт «не внедрён» в gather."""
import datetime as dt

from src.hybrid_report import gather
from src.hybrid_report.models import (
    AppMetricaReviewPrompts,
    ProductReport,
    ProductSpec,
)
from src.hybrid_report.render import _block_review as weekly_block
from src.monthly_report.render import _block_review as monthly_block

_SPEC = ProductSpec(
    key="diktum",
    display="Diktum",
    appmetrica_app_id="6301663",
    onboarding_steps=[("app_open", "открыли")],
    reg_source="diktum",
    review_event="review_prompt_triggered",
)


def _report(rp: AppMetricaReviewPrompts) -> ProductReport:
    return ProductReport(
        spec=_SPEC,
        week_start=dt.date(2026, 7, 1),
        week_end=dt.date(2026, 7, 7),
        review_prompts=rp,
    )


def test_not_implemented_renders_stub():
    line = weekly_block(_report(AppMetricaReviewPrompts(available=False)))
    assert line == "Запрос оценки в приложении: пока не внедрён."
    # месячный рендер даёт тот же текст
    assert monthly_block(_report(AppMetricaReviewPrompts(available=False))) == line


def test_error_degrades_softly():
    rp = AppMetricaReviewPrompts(available=True, error="HTTPError: 500")
    assert weekly_block(_report(rp)) == "Запрос оценки в приложении: данные собираются."


def test_implemented_zero_shows():
    rp = AppMetricaReviewPrompts(available=True, devices=0, events=0)
    assert weekly_block(_report(rp)) == (
        "Запрос оценки в приложении: внедрён, показов пока не было."
    )


def test_devices_and_events_with_plural():
    rp = AppMetricaReviewPrompts(available=True, devices=21, events=23)
    line = weekly_block(_report(rp))
    assert line == "Запрос оценки в приложении: показан 21 устройству (всего 23 раза)."
    # 1 устройство / 1 раз
    one = weekly_block(_report(AppMetricaReviewPrompts(available=True, devices=1, events=1)))
    assert one == "Запрос оценки в приложении: показан 1 устройству (всего 1 раз)."
    # 5 устройств
    five = weekly_block(_report(AppMetricaReviewPrompts(available=True, devices=5, events=5)))
    assert five == "Запрос оценки в приложении: показан 5 устройствам (всего 5 раз)."


def test_by_store_breakdown_appended():
    rp = AppMetricaReviewPrompts(
        available=True, devices=30, events=34,
        by_store=[("App Store", 18), ("Google Play", 9), ("RuStore", 3)],
    )
    line = weekly_block(_report(rp))
    assert line == (
        "Запрос оценки в приложении: показан 30 устройствам (всего 34 раза). "
        "По магазинам: App Store — 18, Google Play — 9, RuStore — 3."
    )
    # месячный рендер — тот же текст
    assert monthly_block(_report(rp)) == line


def test_empty_by_store_no_breakdown_clause():
    rp = AppMetricaReviewPrompts(available=True, devices=5, events=5, by_store=[])
    assert "По магазинам" not in weekly_block(_report(rp))


def test_collect_gate_no_event_skips_appmetrica(monkeypatch):
    """review_event is None → available=False БЕЗ запроса к AppMetrica."""
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("не должно вызываться")

    monkeypatch.setattr(gather.appmetrica, "fetch_review_prompts", _boom)
    spec = ProductSpec(
        key="lapulya", display="Лапуля", appmetrica_app_id="6307939",
        onboarding_steps=[("app_opened_first", "открыли")], reg_source="lapulya",
    )
    res = gather._collect_review_prompts(spec, dt.date(2026, 7, 1), dt.date(2026, 7, 7))
    assert res.available is False
    assert called is False
