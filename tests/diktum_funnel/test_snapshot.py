from __future__ import annotations

import datetime as dt

from src.diktum_funnel import snapshot
from src.diktum_funnel.models import FunnelWeek


def _fw(week_start: dt.date, installs: int, regs: int) -> FunnelWeek:
    return FunnelWeek(
        week_start=week_start, week_end=week_start + dt.timedelta(days=6),
        installs_total=installs, installs_organic=installs, installs_ads=0,
        registrations=regs, activated=0,
    )


def test_store_and_get_prev_week(tmp_path):
    path = tmp_path / "funnel_snapshots.json"
    data = {}
    prev_week = dt.date(2026, 5, 5)
    curr_week = dt.date(2026, 5, 12)
    data = snapshot.store_week(data, _fw(prev_week, installs=10, regs=3))
    snapshot.save(path, data)

    loaded = snapshot.load(path)
    prev = snapshot.get_prev(loaded, curr_week)
    assert prev is not None
    assert prev["installs_total"] == 10
    assert prev["registrations"] == 3


def test_get_prev_missing_returns_none(tmp_path):
    assert snapshot.get_prev({}, dt.date(2026, 5, 12)) is None


def test_load_missing_file_returns_empty(tmp_path):
    assert snapshot.load(tmp_path / "nope.json") == {}
