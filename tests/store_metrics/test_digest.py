"""Digest rendering tests — формат + WoW deltas + alerts."""
from __future__ import annotations

import datetime as dt

from src.store_metrics.digest import render_digest
from src.store_metrics.models import (
    ALERT_PCT,
    ProductReport,
    StoreSnapshot,
    TrendPoint,
    WeekDelta,
    WeeklyReport,
)


def _snap(product, store, installs, rating=4.5, prev=None, error=None):
    return StoreSnapshot(
        product=product, store=store,
        week_start=dt.date(2026, 5, 5),
        installs=installs, rating=rating,
        top_country="RU", top_country_share=0.78,
        error=error,
    )


def _make_report(centry_curr=(30, 15, 5), centry_prev=(20, 22, 3),
                  diktum_curr=(18, 9, 2), diktum_prev=(22, 8, 2),
                  alerts=None):
    """Build a minimal WeeklyReport for rendering."""
    week = dt.date(2026, 5, 5)
    prev_week = week - dt.timedelta(days=7)
    centry = ProductReport(
        product="centry",
        snapshots=[
            _snap("centry", "app_store", centry_curr[0], 4.7),
            _snap("centry", "google_play", centry_curr[1], 4.6),
            _snap("centry", "rustore", centry_curr[2], 4.8),
        ],
        prev_snapshots=[
            StoreSnapshot(product="centry", store="app_store",
                            week_start=prev_week, installs=centry_prev[0], rating=4.7),
            StoreSnapshot(product="centry", store="google_play",
                            week_start=prev_week, installs=centry_prev[1], rating=4.6),
            StoreSnapshot(product="centry", store="rustore",
                            week_start=prev_week, installs=centry_prev[2], rating=4.8),
        ],
        trend_4w=[
            TrendPoint(week_start=prev_week - dt.timedelta(days=21), installs=35),
            TrendPoint(week_start=prev_week - dt.timedelta(days=14), installs=40),
            TrendPoint(week_start=prev_week - dt.timedelta(days=7), installs=45),
            TrendPoint(week_start=week, installs=sum(centry_curr)),
        ],
    )
    diktum = ProductReport(
        product="diktum",
        snapshots=[
            _snap("diktum", "app_store", diktum_curr[0], 4.6),
            _snap("diktum", "google_play", diktum_curr[1], 4.5),
            _snap("diktum", "rustore", diktum_curr[2], 4.7),
        ],
        prev_snapshots=[
            StoreSnapshot(product="diktum", store="app_store",
                            week_start=prev_week, installs=diktum_prev[0], rating=4.6),
            StoreSnapshot(product="diktum", store="google_play",
                            week_start=prev_week, installs=diktum_prev[1], rating=4.5),
            StoreSnapshot(product="diktum", store="rustore",
                            week_start=prev_week, installs=diktum_prev[2], rating=4.7),
        ],
        trend_4w=[],
    )
    return WeeklyReport(week_start=week, products=[centry, diktum],
                         overall_alerts=alerts or [])


# ---------------- WeekDelta ----------------------------------------------

def test_week_delta_positive():
    d = WeekDelta.compute(120, 100)
    assert d.delta_pct == 20.0
    assert d.arrow == "📈"


def test_week_delta_negative_significant():
    d = WeekDelta.compute(80, 100)
    assert d.delta_pct == -20.0
    assert d.arrow == "📉"


def test_week_delta_flat():
    d = WeekDelta.compute(102, 100)
    assert d.arrow == "→"


def test_week_delta_none_prev():
    d = WeekDelta.compute(50, None)
    assert d.arrow == "—"
    assert d.delta_pct is None


def test_week_delta_zero_prev_nonzero_curr():
    d = WeekDelta.compute(10, 0)
    assert d.arrow == "📈"
    assert d.delta_pct is None


def test_week_delta_zero_to_zero():
    d = WeekDelta.compute(0, 0)
    assert d.arrow == "→"
    assert d.delta_pct == 0.0


# ---------------- render_digest ------------------------------------------

def test_render_contains_total_section():
    out = render_digest(_make_report())
    assert "ИТОГО" in out
    # Total = 30+15+5 + 18+9+2 = 79; prev = 20+22+3 + 22+8+2 = 77 → +2.6%
    assert "79" in out
    assert "77" in out


def test_render_per_store_lines_for_centry():
    out = render_digest(_make_report())
    assert "App Store" in out
    assert "Google Play" in out
    assert "RuStore" in out


def test_render_alerts_on_significant_drop():
    """Google Play Centry: 22 → 15 = -31.8% — должен попасть в алерты."""
    out = render_digest(_make_report())
    assert "🚨" in out
    assert "CENTRY" in out and "Google Play" in out
    assert "просел" in out or "просела" in out


def test_render_alerts_skipped_below_threshold():
    """Если Δ < 20% — нет алерта."""
    report = _make_report(
        centry_curr=(22, 21, 5), centry_prev=(20, 22, 5),
        diktum_curr=(20, 9, 2), diktum_prev=(22, 9, 2),
    )
    out = render_digest(report)
    # Никаких значимых падений
    assert "🚨" not in out


def test_render_handles_store_error():
    """StoreSnapshot.error → отдельная строка с error, не в delta."""
    week = dt.date(2026, 5, 5)
    centry = ProductReport(
        product="centry",
        snapshots=[
            _snap("centry", "app_store", 20),
            _snap("centry", "google_play", 10),
            _snap("centry", "rustore", None, error="Stats недоступны через API"),
        ],
        prev_snapshots=[],
        trend_4w=[],
    )
    report = WeeklyReport(week_start=week, products=[centry])
    out = render_digest(report)
    assert "Stats недоступны через API" in out


def test_render_trend_arrow_growing():
    out = render_digest(_make_report())
    # Centry trend: 35 → 40 → 45 → 50 (sum 30+15+5)
    assert "35" in out
    assert "⬆" in out


def test_render_includes_alert_threshold_in_footer():
    out = render_digest(_make_report())
    assert f"{int(ALERT_PCT)}%" in out


def test_render_header_has_week_range():
    out = render_digest(_make_report())
    # week_start=2026-05-05 (Mon) → ends 11.05.2026
    assert "05.05" in out
    assert "11.05" in out


# ---------------- Hypotheses section (METRICS-09 / D-5-06) ----------------

def _make_report_with_hypotheses(hypotheses: list[str]):
    """Build minimal WeeklyReport carrying given hypotheses.

    Re-uses _make_report() but injects hypotheses via dataclasses.replace
    (WeeklyReport is frozen).
    """
    import dataclasses as _dc
    base = _make_report()
    return _dc.replace(base, hypotheses=hypotheses)


def test_render_digest_with_hypotheses_includes_section():
    """When hypotheses non-empty → «💡 Гипотезы недели» header + all bullets."""
    out = render_digest(_make_report_with_hypotheses([
        "test insight 1",
        "test insight 2",
    ]))
    assert "💡 Гипотезы недели" in out
    assert "test insight 1" in out
    assert "test insight 2" in out


def test_render_digest_empty_hypotheses_omits_section():
    """Default hypotheses=[] → no '💡' header in digest (existing behaviour)."""
    # _make_report() does not pass hypotheses → defaults to []
    out = render_digest(_make_report())
    assert "💡" not in out
    assert "Гипотезы недели" not in out


def test_render_digest_hypotheses_section_placement():
    """Section sits BETWEEN alerts (🚨) and footer (<i>Собрано ...).

    Uses _make_report() default — Centry GP -31.8% generates an alert, so
    alerts section is present and we can verify ordering.
    """
    out = render_digest(_make_report_with_hypotheses(["sandwich insight"]))
    alerts_idx = out.index("🚨 Алерты")
    hypo_idx = out.index("💡 Гипотезы недели")
    footer_idx = out.index("<i>Собрано")
    assert alerts_idx < hypo_idx < footer_idx


def test_render_digest_hypotheses_bullet_format():
    """Each insight prefixed with «• » (bullet + space)."""
    out = render_digest(_make_report_with_hypotheses([
        "alpha",
        "beta gamma",
    ]))
    assert "• alpha" in out
    assert "• beta gamma" in out


def test_render_digest_hypotheses_section_works_without_alerts():
    """If no alerts but hypotheses present → section still rendered, no crash."""
    # Use flat-delta variant — no alerts fire
    report = _make_report(
        centry_curr=(22, 21, 5), centry_prev=(20, 22, 5),
        diktum_curr=(20, 9, 2), diktum_prev=(22, 9, 2),
    )
    import dataclasses as _dc
    report = _dc.replace(report, hypotheses=["only insight"])
    out = render_digest(report)
    assert "🚨" not in out
    assert "💡 Гипотезы недели" in out
    assert "• only insight" in out
    # Still rendered before footer
    assert out.index("💡 Гипотезы недели") < out.index("<i>Собрано")
