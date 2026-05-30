from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src.hybrid_report import cli
from src.hybrid_report.models import PRODUCTS, ProductReport


def _fake_report(spec) -> ProductReport:
    return ProductReport(
        spec=spec,
        week_start=dt.date(2026, 5, 25),
        week_end=dt.date(2026, 5, 31),
        am_installs_total=10,
    )


def test_report_week_is_previous_iso_week():
    # Пн 2026-06-01 → отчёт за прошлую ISO-неделю 2026-05-25…05-31
    ws, we = cli._report_week(dt.date(2026, 6, 1))
    assert ws == dt.date(2026, 5, 25)
    assert we == dt.date(2026, 5, 31)


def test_main_sends_two_messages(tmp_path):
    snap = tmp_path / "hybrid_snapshots.json"
    with (
        patch.object(cli.gather, "gather_product",
                     side_effect=lambda spec, ws, we, data: _fake_report(spec)),
        patch.object(cli, "send_to_planner", return_value=True) as send,
    ):
        rc = cli.main(today=dt.date(2026, 6, 1), snapshots_path=snap, dry_run=False)
    assert rc == 0
    # ОТДЕЛЬНОЕ сообщение на каждый продукт → ровно len(PRODUCTS) вызовов
    assert send.call_count == len(PRODUCTS) == 2
    # снапшот сохранён (не dry-run)
    assert snap.exists()


def test_main_dry_run_prints_not_sends(tmp_path, capsys):
    snap = tmp_path / "hybrid_snapshots.json"
    with (
        patch.object(cli.gather, "gather_product",
                     side_effect=lambda spec, ws, we, data: _fake_report(spec)),
        patch.object(cli, "send_to_planner", return_value=True) as send,
    ):
        rc = cli.main(today=dt.date(2026, 6, 1), snapshots_path=snap, dry_run=True)
    assert rc == 0
    send.assert_not_called()
    out = capsys.readouterr().out
    assert "Centry — отчёт за неделю" in out
    assert "Diktum — отчёт за неделю" in out
    # снапшот НЕ сохраняется в dry-run
    assert not snap.exists()


def test_main_returns_1_if_send_fails(tmp_path):
    snap = tmp_path / "hybrid_snapshots.json"
    with (
        patch.object(cli.gather, "gather_product",
                     side_effect=lambda spec, ws, we, data: _fake_report(spec)),
        patch.object(cli, "send_to_planner", return_value=False) as send,
    ):
        rc = cli.main(today=dt.date(2026, 6, 1), snapshots_path=snap, dry_run=False)
    # оба продукта всё равно обработаны (loop не прерывается), но rc=1
    assert send.call_count == 2
    assert rc == 1


def test_send_to_planner_no_creds_returns_false(monkeypatch):
    monkeypatch.delenv("TG_PLANNER_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_OWNER_CHAT_ID", raising=False)
    assert cli.send_to_planner("hi") is False


def test_send_to_planner_no_html_parse_mode(monkeypatch):
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(cli.requests, "post", _fake_post)
    assert cli.send_to_planner("plain text") is True
    # plain text → parse_mode НЕ задан
    assert "parse_mode" not in captured["json"]
    assert captured["json"]["disable_web_page_preview"] is True
