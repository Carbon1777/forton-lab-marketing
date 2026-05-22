from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src.centry_funnel import cli
from src.centry_funnel.appmetrica import InstallsBySource
from src.centry_funnel.supabase_src import FunnelDB


def test_report_week_is_previous_iso_week():
    # вторник 19 мая 2026 → отчёт за пред. неделю Пн 11 – Вс 17 мая
    ws, we = cli._report_week(dt.date(2026, 5, 19))
    assert ws == dt.date(2026, 5, 11)
    assert we == dt.date(2026, 5, 17)


def test_main_collects_renders_sends_saves(tmp_path):
    snap = tmp_path / "centry_funnel_snapshots.json"
    with patch.object(cli.appmetrica, "fetch_installs",
                      return_value=InstallsBySource(18, 16, 2, {"Органика": 16})), \
         patch.object(cli.supabase_src, "fetch_funnel",
                      return_value=FunnelDB(new_profiles=7, guests=5, users=2, activations=1)), \
         patch.object(cli, "send_to_planner", return_value=True) as send:
        rc = cli.main(today=dt.date(2026, 5, 19), snapshots_path=snap)
    assert rc == 0
    send.assert_called_once()
    sent_text = send.call_args.args[0]
    assert "Centry" in sent_text and "18" in sent_text
    assert snap.exists()


def test_main_graceful_when_appmetrica_fails(tmp_path):
    snap = tmp_path / "centry_funnel_snapshots.json"
    with patch.object(cli.appmetrica, "fetch_installs",
                      side_effect=RuntimeError("401 token")), \
         patch.object(cli.supabase_src, "fetch_funnel",
                      return_value=FunnelDB(new_profiles=7, guests=5, users=2, activations=1)), \
         patch.object(cli, "send_to_planner", return_value=True) as send:
        rc = cli.main(today=dt.date(2026, 5, 19), snapshots_path=snap)
    assert rc == 0           # отчёт всё равно ушёл
    sent_text = send.call_args.args[0]
    assert "—" in sent_text  # установки прочерком
