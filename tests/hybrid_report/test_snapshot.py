from __future__ import annotations

import datetime as dt

from src.hybrid_report import snapshot


def test_store_save_load_get_prev_round_trip(tmp_path):
    path = tmp_path / "hybrid_snapshots.json"
    data: dict = {}
    # неделя W21 (2026-05-18 пн)
    w_prev = dt.date(2026, 5, 18)
    snapshot.store_week(data, w_prev, "centry", 24)
    snapshot.store_week(data, w_prev, "diktum", 121)
    snapshot.save(path, data)

    loaded = snapshot.load(path)
    # текущая неделя W22 (2026-05-25) → prev = W21
    w_cur = dt.date(2026, 5, 25)
    assert snapshot.get_prev_installs(loaded, w_cur, "centry") == 24
    assert snapshot.get_prev_installs(loaded, w_cur, "diktum") == 121


def test_get_prev_missing_is_none(tmp_path):
    data: dict = {}
    w_cur = dt.date(2026, 5, 25)
    assert snapshot.get_prev_installs(data, w_cur, "centry") is None
    # неделя есть, продукта нет
    snapshot.store_week(data, dt.date(2026, 5, 18), "diktum", 5)
    assert snapshot.get_prev_installs(data, w_cur, "centry") is None


def test_load_missing_file_returns_empty(tmp_path):
    assert snapshot.load(tmp_path / "nope.json") == {}


def test_prune_keeps_last_8_weeks(tmp_path):
    path = tmp_path / "hybrid_snapshots.json"
    data: dict = {}
    # 12 недель назад → сейчас
    base = dt.date(2026, 1, 5)  # понедельник
    for i in range(12):
        wk = base + dt.timedelta(days=7 * i)
        snapshot.store_week(data, wk, "centry", i)
    snapshot.save(path, data)
    loaded = snapshot.load(path)
    assert len(loaded) == 8
    # самые старые отброшены, новейшая (i=11) на месте
    newest = base + dt.timedelta(days=7 * 11)
    key = snapshot._week_key(newest)
    assert loaded[key]["centry"]["am_installs_total"] == 11


def test_store_week_none_installs(tmp_path):
    data: dict = {}
    w = dt.date(2026, 5, 18)
    snapshot.store_week(data, w, "centry", None)
    w_cur = dt.date(2026, 5, 25)
    assert snapshot.get_prev_installs(data, w_cur, "centry") is None
