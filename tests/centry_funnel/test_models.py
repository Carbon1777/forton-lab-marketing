from __future__ import annotations

import datetime as dt

from src.centry_funnel.models import FunnelWeek


def test_funnel_week_holds_all_fields():
    fw = FunnelWeek(
        week_start=dt.date(2026, 5, 12),
        week_end=dt.date(2026, 5, 18),
        installs_total=18, installs_organic=16, installs_ads=2,
        new_profiles=7, guests=5, users=2, activations=1,
    )
    assert fw.installs_total == 18
    assert fw.new_profiles == 7
    assert fw.guests == 5
    assert fw.users == 2
    assert fw.activations == 1
    assert fw.appmetrica_error is None
    assert fw.supabase_error is None


def test_funnel_week_allows_none_on_source_failure():
    fw = FunnelWeek(
        week_start=dt.date(2026, 5, 12),
        week_end=dt.date(2026, 5, 18),
        installs_total=None, installs_organic=None, installs_ads=None,
        new_profiles=7, guests=5, users=2, activations=1,
        appmetrica_error="401 token expired",
    )
    assert fw.installs_total is None
    assert fw.appmetrica_error == "401 token expired"
