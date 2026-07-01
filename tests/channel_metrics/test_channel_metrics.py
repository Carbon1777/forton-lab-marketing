"""Tests for src/channel_metrics — weekly channel-analytics digest.

Внешние HTTP замоканы через unittest.mock (patch fetch_with_retry / fetch_weekly),
как в остальном сюите (без `responses`). Ни один тест не ходит в сеть.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from src.channel_metrics import digest as digest_mod
from src.channel_metrics import snapshot as snap_mod
from src.channel_metrics import telegram, vk, youtube
from src.channel_metrics.cli import main
from src.channel_metrics.models import (
    ChannelDelta,
    ChannelReport,
    ChannelSnapshot,
)

# Понедельник ISO-недели 2026-W27.
WEEK = dt.date(2026, 6, 29)
PREV_WEEK = WEEK - dt.timedelta(days=7)


def _resp(payload: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.json.return_value = payload
    m.status_code = status
    return m


# --------------------------------------------------------------------------
# models — ChannelDelta
# --------------------------------------------------------------------------
def test_delta_up():
    d = ChannelDelta.compute(100, 90)
    assert d.delta_abs == 10 and d.arrow == "📈"


def test_delta_down():
    d = ChannelDelta.compute(90, 100)
    assert d.delta_abs == -10 and d.arrow == "📉"


def test_delta_flat():
    d = ChannelDelta.compute(100, 100)
    assert d.delta_abs == 0 and d.arrow == "→"


def test_delta_none_previous():
    d = ChannelDelta.compute(100, None)
    assert d.delta_abs is None and d.arrow == "—"


# --------------------------------------------------------------------------
# telegram fetcher
# --------------------------------------------------------------------------
def test_tg_no_token(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    snap = telegram.fetch_weekly(WEEK)
    assert snap.subscribers is None and "TG_BOT_TOKEN" in snap.error


@patch("src.channel_metrics.telegram.fetch_with_retry")
def test_tg_ok(mock_fetch, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "x")
    mock_fetch.return_value = _resp({"ok": True, "result": 1234})
    snap = telegram.fetch_weekly(WEEK)
    assert snap.subscribers == 1234 and snap.error is None


@patch("src.channel_metrics.telegram.fetch_with_retry")
def test_tg_api_error(mock_fetch, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "x")
    mock_fetch.return_value = _resp({"ok": False, "description": "chat not found"})
    snap = telegram.fetch_weekly(WEEK)
    assert snap.subscribers is None and "chat not found" in snap.error


@patch("src.channel_metrics.telegram.fetch_with_retry", side_effect=RuntimeError("boom"))
def test_tg_exception_degrades(mock_fetch, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "x")
    snap = telegram.fetch_weekly(WEEK)
    assert snap.subscribers is None and "RuntimeError" in snap.error


# --------------------------------------------------------------------------
# vk fetcher
# --------------------------------------------------------------------------
@patch("src.channel_metrics.vk.fetch_with_retry")
def test_vk_members_and_reach(mock_fetch, monkeypatch):
    monkeypatch.setenv("VK_GROUP_TOKEN", "t")
    monkeypatch.setenv("VK_GROUP_ID", "238188721")
    mock_fetch.side_effect = [
        _resp({"response": {"groups": [{"id": 1, "members_count": 567}]}}),
        _resp({"response": [{"reach": {"reach": 8900}}]}),
    ]
    snap = vk.fetch_weekly(WEEK)
    assert snap.subscribers == 567 and snap.reach == 8900


@patch("src.channel_metrics.vk.fetch_with_retry")
def test_vk_reach_degrades_on_stats_error(mock_fetch, monkeypatch):
    monkeypatch.setenv("VK_GROUP_TOKEN", "t")
    monkeypatch.setenv("VK_GROUP_ID", "238188721")
    mock_fetch.side_effect = [
        _resp({"response": {"groups": [{"members_count": 567}]}}),
        _resp({"error": {"error_code": 15, "error_msg": "Access denied"}}),
    ]
    snap = vk.fetch_weekly(WEEK)
    assert snap.subscribers == 567 and snap.reach is None and snap.error is None


@patch("src.channel_metrics.vk.fetch_with_retry")
def test_vk_old_list_format(mock_fetch, monkeypatch):
    monkeypatch.setenv("VK_GROUP_TOKEN", "t")
    monkeypatch.setenv("VK_GROUP_ID", "1")
    mock_fetch.side_effect = [
        _resp({"response": [{"members_count": 100}]}),
        _resp({"error": {"error_msg": "no stats"}}),
    ]
    snap = vk.fetch_weekly(WEEK)
    assert snap.subscribers == 100


@patch("src.channel_metrics.vk.fetch_with_retry")
def test_vk_members_error(mock_fetch, monkeypatch):
    monkeypatch.setenv("VK_GROUP_TOKEN", "t")
    monkeypatch.setenv("VK_GROUP_ID", "1")
    mock_fetch.side_effect = [_resp({"error": {"error_msg": "bad token"}})]
    snap = vk.fetch_weekly(WEEK)
    assert snap.subscribers is None and "bad token" in snap.error


def test_vk_no_creds(monkeypatch):
    monkeypatch.delenv("VK_GROUP_TOKEN", raising=False)
    monkeypatch.delenv("VK_GROUP_ID", raising=False)
    snap = vk.fetch_weekly(WEEK)
    assert snap.subscribers is None and "VK_GROUP_TOKEN" in snap.error


# --------------------------------------------------------------------------
# youtube fetcher (optional)
# --------------------------------------------------------------------------
def test_yt_optional_absent(monkeypatch):
    monkeypatch.delenv("YT_API_KEY", raising=False)
    monkeypatch.delenv("YT_CHANNEL_ID", raising=False)
    snap = youtube.fetch_weekly(WEEK)
    assert snap.subscribers is None and "optional" in snap.error


@patch("src.channel_metrics.youtube.fetch_with_retry")
def test_yt_ok(mock_fetch, monkeypatch):
    monkeypatch.setenv("YT_API_KEY", "k")
    monkeypatch.setenv("YT_CHANNEL_ID", "UC123")
    mock_fetch.return_value = _resp(
        {"items": [{"statistics": {"subscriberCount": "89"}}]}
    )
    snap = youtube.fetch_weekly(WEEK)
    assert snap.subscribers == 89 and snap.error is None


@patch("src.channel_metrics.youtube.fetch_with_retry")
def test_yt_channel_not_found(mock_fetch, monkeypatch):
    monkeypatch.setenv("YT_API_KEY", "k")
    monkeypatch.setenv("YT_CHANNEL_ID", "UCbad")
    mock_fetch.return_value = _resp({"items": []})
    snap = youtube.fetch_weekly(WEEK)
    assert snap.subscribers is None and "not found" in snap.error


# --------------------------------------------------------------------------
# snapshot persistence
# --------------------------------------------------------------------------
def test_snapshot_roundtrip(tmp_path):
    p = tmp_path / ".metrics" / "channel_snapshots.json"
    snaps = [
        ChannelSnapshot("telegram", WEEK, 1234),
        ChannelSnapshot("vk", WEEK, 567, reach=8900),
    ]
    data = snap_mod.store_week({}, snaps)
    snap_mod.save(p, data)
    assert p.exists()

    loaded = snap_mod.load(p)
    prev = snap_mod.get_prev_week(loaded, WEEK + dt.timedelta(days=7))
    by = {s.platform: s for s in prev}
    assert by["telegram"].subscribers == 1234
    assert by["vk"].reach == 8900


def test_snapshot_prune_keeps_8():
    data = {f"2026-W{i:02d}": {"telegram": {}} for i in range(1, 12)}  # 11 weeks
    pruned = snap_mod._prune(data, 8)
    assert len(pruned) == 8
    # оставлены последние (по сортировке) недели
    assert "2026-W11" in pruned and "2026-W01" not in pruned


def test_snapshot_load_missing(tmp_path):
    assert snap_mod.load(tmp_path / "nope.json") == {}


# --------------------------------------------------------------------------
# digest rendering
# --------------------------------------------------------------------------
def _sample_report() -> ChannelReport:
    return ChannelReport(
        week_start=WEEK,
        snapshots=[
            ChannelSnapshot("telegram", WEEK, 1234),
            ChannelSnapshot("vk", WEEK, 567, reach=8900),
            ChannelSnapshot(
                "youtube", WEEK, None,
                error="YT_API_KEY/YT_CHANNEL_ID not set (optional)",
            ),
        ],
        prev_snapshots=[ChannelSnapshot("telegram", PREV_WEEK, 1222)],
    )


def test_digest_contains_channels_and_delta():
    out = digest_mod.render_channel_digest(_sample_report())
    assert "Telegram" in out and "1 234" in out and "+12" in out
    assert "охват: 8 900" in out
    assert "optional" in out             # youtube unavailable row shows its reason
    assert "<b>" in out                  # HTML header
    assert out.strip().endswith("</i>")  # footer


def test_digest_escapes_html_in_error():
    report = ChannelReport(
        week_start=WEEK,
        snapshots=[ChannelSnapshot("telegram", WEEK, None, error="<script>x</script>")],
        prev_snapshots=[],
    )
    out = digest_mod.render_channel_digest(report)
    assert "&lt;script&gt;" in out and "<script>" not in out


# --------------------------------------------------------------------------
# cli.main — orchestration
# --------------------------------------------------------------------------
@patch("src.channel_metrics.cli.send_to_planner", return_value=True)
@patch("src.channel_metrics.youtube.fetch_weekly")
@patch("src.channel_metrics.vk.fetch_weekly")
@patch("src.channel_metrics.telegram.fetch_weekly")
def test_main_happy_path(m_tg, m_vk, m_yt, m_send, tmp_path):
    m_tg.return_value = ChannelSnapshot("telegram", WEEK, 1000)
    m_vk.return_value = ChannelSnapshot("vk", WEEK, 500, reach=800)
    m_yt.return_value = ChannelSnapshot("youtube", WEEK, None, error="optional")

    snap_path = tmp_path / ".metrics" / "channel_snapshots.json"
    rc = main(today=WEEK, snapshots_path=snap_path)

    assert rc == 0
    m_send.assert_called_once()
    assert snap_path.exists()
    data = snap_mod.load(snap_path)
    # снимок сохранён под текущей ISO-неделей
    key = snap_mod._week_key(WEEK)
    assert key in data and data[key]["telegram"]["subscribers"] == 1000


@patch("src.channel_metrics.cli.send_to_planner", return_value=False)
@patch("src.channel_metrics.youtube.fetch_weekly")
@patch("src.channel_metrics.vk.fetch_weekly")
@patch("src.channel_metrics.telegram.fetch_weekly")
def test_main_send_failure_returns_1_but_saves(m_tg, m_vk, m_yt, m_send, tmp_path):
    m_tg.return_value = ChannelSnapshot("telegram", WEEK, 1000)
    m_vk.return_value = ChannelSnapshot("vk", WEEK, 500)
    m_yt.return_value = ChannelSnapshot("youtube", WEEK, None, error="optional")
    snap_path = tmp_path / ".metrics" / "channel_snapshots.json"
    rc = main(today=WEEK, snapshots_path=snap_path)
    assert rc == 1  # send failed
    assert snap_path.exists()  # снапшот всё равно сохранён


@patch("src.channel_metrics.cli.send_to_planner", return_value=True)
@patch("src.channel_metrics.youtube.fetch_weekly")
@patch("src.channel_metrics.vk.fetch_weekly")
@patch("src.channel_metrics.telegram.fetch_weekly")
def test_main_wow_delta_across_two_runs(m_tg, m_vk, m_yt, m_send, tmp_path):
    snap_path = tmp_path / ".metrics" / "channel_snapshots.json"
    # неделя 1
    m_tg.return_value = ChannelSnapshot("telegram", PREV_WEEK, 1000)
    m_vk.return_value = ChannelSnapshot("vk", PREV_WEEK, 500)
    m_yt.return_value = ChannelSnapshot("youtube", PREV_WEEK, None, error="optional")
    main(today=PREV_WEEK, snapshots_path=snap_path)
    # неделя 2 — prev-неделя должна найтись → дайджест покажет дельту
    m_tg.return_value = ChannelSnapshot("telegram", WEEK, 1050)
    m_vk.return_value = ChannelSnapshot("vk", WEEK, 500)
    m_yt.return_value = ChannelSnapshot("youtube", WEEK, None, error="optional")
    with patch("src.channel_metrics.cli.render_channel_digest",
               wraps=digest_mod.render_channel_digest) as m_render:
        main(today=WEEK, snapshots_path=snap_path)
        report = m_render.call_args.args[0]
    prev_by = {s.platform: s for s in report.prev_snapshots}
    assert prev_by["telegram"].subscribers == 1000  # нашёл прошлую неделю
