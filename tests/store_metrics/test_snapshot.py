"""Snapshot persistence — load/save/prune/get_prev_week/get_4w_trend."""
from __future__ import annotations

import datetime as dt
import json

from src.store_metrics.models import StoreSnapshot
from src.store_metrics.snapshot import (
    MAX_WEEKS_KEPT,
    _iso_week_start,
    _prune,
    _week_key,
    get_4w_trend,
    get_prev_week,
    load,
    save,
    store_week,
)


def _mk_snap(product, store, week_start, installs=10):
    return StoreSnapshot(
        product=product, store=store,
        week_start=week_start, installs=installs,
    )


def test_iso_week_start_monday():
    """4 мая 2026 = Понедельник, ожидаем 4 мая."""
    assert _iso_week_start(dt.date(2026, 5, 4)) == dt.date(2026, 5, 4)


def test_iso_week_start_tuesday():
    """5 мая 2026 = Вторник, ожидаем 4 мая (Mon)."""
    assert _iso_week_start(dt.date(2026, 5, 5)) == dt.date(2026, 5, 4)


def test_load_returns_empty_when_no_file(tmp_path):
    assert load(tmp_path / "nope.json") == {}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "snap.json"
    data = {"2026-W19": {"centry": {"app_store": {"installs": 5}}}}
    save(path, data)
    assert load(path) == data


def test_store_week_creates_keyed_buckets(tmp_path):
    snaps = [
        _mk_snap("centry", "app_store", dt.date(2026, 5, 5), 20),
        _mk_snap("centry", "google_play", dt.date(2026, 5, 5), 15),
        _mk_snap("diktum", "rustore", dt.date(2026, 5, 5), 3),
    ]
    data = store_week({}, snaps)
    week_key = _week_key(dt.date(2026, 5, 5))
    assert week_key in data
    assert "centry" in data[week_key]
    assert "diktum" in data[week_key]
    assert data[week_key]["centry"]["app_store"]["installs"] == 20


def test_get_prev_week_returns_snapshots(tmp_path):
    current = dt.date(2026, 5, 12)
    prev = dt.date(2026, 5, 5)
    snaps_prev = [
        _mk_snap("centry", "app_store", prev, 20),
        _mk_snap("centry", "google_play", prev, 16),
    ]
    data = store_week({}, snaps_prev)
    result = get_prev_week(data, current, "centry")
    assert len(result) == 2
    assert sum(s.installs for s in result) == 36


def test_get_prev_week_empty_if_no_data():
    assert get_prev_week({}, dt.date(2026, 5, 12), "centry") == []


def test_get_4w_trend_aggregates_across_stores():
    current = dt.date(2026, 5, 12)
    weeks = [
        dt.date(2026, 4, 21),
        dt.date(2026, 4, 28),
        dt.date(2026, 5, 5),
    ]
    data = {}
    for w, installs in zip(weeks, [30, 40, 45]):
        snaps = [
            _mk_snap("centry", "app_store", w, installs // 2),
            _mk_snap("centry", "google_play", w, installs - installs // 2),
        ]
        data = store_week(data, snaps)
    trend = get_4w_trend(data, current, "centry")
    assert len(trend) == 4
    # Last point — current week, not stored yet
    assert trend[-1].installs is None
    # W-1, W-2, W-3 — stored
    assert trend[-2].installs == 45
    assert trend[-3].installs == 40
    assert trend[-4].installs == 30


def test_prune_keeps_last_n_weeks():
    data = {f"2026-W{w:02d}": {} for w in range(1, 20)}
    pruned = _prune(data, 8)
    assert len(pruned) == 8
    weeks = sorted(pruned.keys())
    assert weeks[-1] == "2026-W19"
    assert weeks[0] == "2026-W12"


def test_save_prunes_old_weeks(tmp_path):
    """save() автоматом обрезает старые недели до MAX_WEEKS_KEPT."""
    path = tmp_path / "snap.json"
    data = {f"2026-W{w:02d}": {} for w in range(1, 20)}
    save(path, data)
    reloaded = load(path)
    assert len(reloaded) == MAX_WEEKS_KEPT
