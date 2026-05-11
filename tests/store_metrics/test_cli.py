"""CLI integration tests — collect_all + build_report + main flow."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src.store_metrics import cli
from src.store_metrics.models import StoreSnapshot


def test_collect_all_returns_snapshots_in_mock_mode(monkeypatch):
    """Без secrets все 3 adapter'a возвращают mocks → 6 снапшотов."""
    for v in ("ASC_KEY_ID", "GOOGLE_PLAY_SA_JSON", "RUSTORE_PRIVATE_KEY"):
        monkeypatch.delenv(v, raising=False)
    snaps = cli.collect_all(dt.date(2026, 5, 5))
    assert len(snaps) == 6   # 2 products × 3 stores
    products = {s.product for s in snaps}
    stores = {s.store for s in snaps}
    assert products == {"centry", "diktum"}
    assert stores == {"app_store", "google_play", "rustore"}
    # mock data → all have installs
    assert all(s.installs is not None for s in snaps)


def test_build_report_attaches_prev_snapshots():
    week = dt.date(2026, 5, 12)
    prev_week = dt.date(2026, 5, 5)
    prev_snaps = [
        StoreSnapshot(product="centry", store="app_store",
                        week_start=prev_week, installs=18),
    ]
    data = {}
    from src.store_metrics.snapshot import store_week
    data = store_week(data, prev_snaps)

    current = [
        StoreSnapshot(product="centry", store="app_store",
                        week_start=week, installs=25),
        StoreSnapshot(product="diktum", store="app_store",
                        week_start=week, installs=10),
    ]
    report = cli.build_report(week, data, current)
    assert report.week_start == week
    centry = [p for p in report.products if p.product == "centry"][0]
    assert len(centry.prev_snapshots) == 1
    assert centry.prev_snapshots[0].installs == 18


def test_main_in_mock_mode_writes_snapshot(tmp_path, monkeypatch):
    """main() runs end-to-end with mocks: produces digest, saves snapshot."""
    for v in ("ASC_KEY_ID", "GOOGLE_PLAY_SA_JSON", "RUSTORE_PRIVATE_KEY",
                "TG_PLANNER_BOT_TOKEN", "TG_OWNER_CHAT_ID"):
        monkeypatch.delenv(v, raising=False)
    snap_path = tmp_path / "snap.json"
    today = dt.date(2026, 5, 12)   # Tuesday — last week = May 4-10
    rc = cli.main(today=today, snapshots_path=snap_path)
    # send_to_planner returns False without TG creds → rc=1, но snapshot всё равно сохранён
    assert rc in (0, 1)
    assert snap_path.exists()
    import json as _j
    data = _j.loads(snap_path.read_text())
    assert any("centry" in v for v in data.values())


def test_send_to_planner_returns_false_without_creds(monkeypatch):
    monkeypatch.delenv("TG_PLANNER_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_OWNER_CHAT_ID", raising=False)
    assert cli.send_to_planner("test") is False


def test_send_to_planner_success(monkeypatch):
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")
    with patch("src.store_metrics.cli.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        assert cli.send_to_planner("test digest") is True
    sent = mock_post.call_args.kwargs["json"]
    assert sent["text"] == "test digest"
    assert sent["parse_mode"] == "HTML"
