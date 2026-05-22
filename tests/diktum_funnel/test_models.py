from __future__ import annotations

import datetime as dt

from src.diktum_funnel.models import FunnelWeek


def test_funnel_week_holds_all_fields():
    fw = FunnelWeek(
        week_start=dt.date(2026, 5, 12),
        week_end=dt.date(2026, 5, 18),
        installs_total=18, installs_organic=16, installs_ads=2,
        registrations=4, activated=2,
    )
    assert fw.installs_total == 18
    assert fw.installs_ads == 2
    assert fw.registrations == 4
    assert fw.activated == 2
    assert fw.appmetrica_error is None
    assert fw.supabase_error is None


def test_funnel_week_allows_none_on_source_failure():
    fw = FunnelWeek(
        week_start=dt.date(2026, 5, 12),
        week_end=dt.date(2026, 5, 18),
        installs_total=None, installs_organic=None, installs_ads=None,
        registrations=4, activated=2,
        appmetrica_error="401 token expired",
    )
    assert fw.installs_total is None
    assert fw.appmetrica_error == "401 token expired"
