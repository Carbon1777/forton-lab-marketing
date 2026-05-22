from __future__ import annotations

import datetime as dt

from src.centry_funnel import snapshot
from src.centry_funnel.models import FunnelWeek


def _fw(week_start: dt.date, installs: int, new_profiles: int) -> FunnelWeek:
    return FunnelWeek(
        week_start=week_start, week_end=week_start + dt.timedelta(days=6),
        installs_total=installs, installs_organic=installs, installs_ads=0,
        new_profiles=new_profiles, guests=new_profiles, users=0, activations=0,
    )


def test_store_and_get_prev_week(tmp_path):
    path = tmp_path / "centry_funnel_snapshots.json"
    data = {}
    prev_week = dt.date(2026, 5, 5)
    curr_week = dt.date(2026, 5, 12)
    data = snapshot.store_week(data, _fw(prev_week, installs=10, new_profiles=3))
    snapshot.save(path, data)

    loaded = snapshot.load(path)
    prev = snapshot.get_prev(loaded, curr_week)
    assert prev is not None
    assert prev["installs_total"] == 10
    assert prev["new_profiles"] == 3


def test_get_prev_missing_returns_none():
    assert snapshot.get_prev({}, dt.date(2026, 5, 12)) is None


def test_load_missing_file_returns_empty(tmp_path):
    assert snapshot.load(tmp_path / "nope.json") == {}
