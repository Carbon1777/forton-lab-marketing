"""CLI integration tests — collect_all + build_report + main flow."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.store_metrics import cli
from src.store_metrics.models import StoreSnapshot


def test_collect_all_returns_snapshots_in_mock_mode(monkeypatch):
    """Без secrets все 3 adapter'a возвращают mocks → 6 снапшотов."""
    for v in (
        # New Apple Reporter envs (wired 2026-05-14, replaces ASC_KEY_ID etc.)
        "ASC_REPORTER_ACCESS_TOKEN", "ASC_VENDOR_NUMBER",
        "ASC_APP_ID_CENTRY", "ASC_APP_ID_DIKTUM",
        # Legacy / other-store
        "ASC_KEY_ID", "GOOGLE_PLAY_SA_JSON", "RUSTORE_PRIVATE_KEY",
    ):
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
    for v in (
        "ASC_REPORTER_ACCESS_TOKEN", "ASC_VENDOR_NUMBER",
        "ASC_APP_ID_CENTRY", "ASC_APP_ID_DIKTUM",
        "ASC_KEY_ID", "GOOGLE_PLAY_SA_JSON", "RUSTORE_PRIVATE_KEY",
        "TG_PLANNER_BOT_TOKEN", "TG_OWNER_CHAT_ID",
        # Hypothesis gate — strip API key so generate() short-circuits to []
        # without attempting a network call (hermetic test).
        "ANTHROPIC_API_KEY",
    ):
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


# ---------------- Hypothesis injection (METRICS-09 / D-5-06) ----------------

def _strip_default_env(monkeypatch):
    """Helper: strip every env var that affects mock-mode adapter behaviour
    and the hypothesis gate. Used by every hypothesis-injection test."""
    for v in (
        "ASC_REPORTER_ACCESS_TOKEN", "ASC_VENDOR_NUMBER",
        "ASC_APP_ID_CENTRY", "ASC_APP_ID_DIKTUM",
        "ASC_KEY_ID", "GOOGLE_PLAY_SA_JSON", "RUSTORE_PRIVATE_KEY",
        "TG_PLANNER_BOT_TOKEN", "TG_OWNER_CHAT_ID",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(v, raising=False)


def test_main_injects_hypotheses_into_report(tmp_path, monkeypatch):
    """Mock hypothesis.generate → digest sent to send_to_planner contains insights."""
    _strip_default_env(monkeypatch)
    # Set TG creds so send_to_planner actually fires (and we can capture digest)
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")

    snap_path = tmp_path / "snap.json"
    today = dt.date(2026, 5, 12)

    captured_digest: dict = {}

    def _capture(digest_text: str) -> bool:
        captured_digest["text"] = digest_text
        return True

    with patch.object(
        cli.hypothesis, "generate",
        return_value=["mock insight 1", "mock insight 2"],
    ) as mock_gen, patch.object(cli, "send_to_planner", side_effect=_capture):
        rc = cli.main(today=today, snapshots_path=snap_path)

    # hypothesis.generate called exactly once, with WeeklyReport + spend_file
    mock_gen.assert_called_once()
    call_args, call_kwargs = mock_gen.call_args
    # Positional report arg
    assert call_args[0].week_start == dt.date(2026, 5, 4)
    # spend_file kwarg points at marketing-v3/.metrics/api_spend.json
    assert "spend_file" in call_kwargs
    assert call_kwargs["spend_file"].name == "api_spend.json"

    # Digest passed to TG actually contains the mocked insights
    assert "💡 Гипотезы недели" in captured_digest["text"]
    assert "• mock insight 1" in captured_digest["text"]
    assert "• mock insight 2" in captured_digest["text"]
    assert rc == 0


def test_main_hypothesis_empty_no_section_in_digest(tmp_path, monkeypatch):
    """Mock hypothesis.generate returns [] → digest does NOT contain '💡 Гипотезы'."""
    _strip_default_env(monkeypatch)
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")

    snap_path = tmp_path / "snap.json"
    today = dt.date(2026, 5, 12)

    captured_digest: dict = {}

    def _capture(digest_text: str) -> bool:
        captured_digest["text"] = digest_text
        return True

    with patch.object(cli.hypothesis, "generate", return_value=[]), \
         patch.object(cli, "send_to_planner", side_effect=_capture):
        cli.main(today=today, snapshots_path=snap_path)

    assert "💡" not in captured_digest["text"]
    assert "Гипотезы недели" not in captured_digest["text"]


def test_main_uses_replace_for_frozen_dataclass(tmp_path, monkeypatch):
    """WeeklyReport is frozen=True → must use dataclasses.replace (not mutation).

    Verifies the report passed to render_digest has hypotheses set, without
    any AttributeError from attempted attribute assignment on a frozen instance.
    """
    _strip_default_env(monkeypatch)
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")

    snap_path = tmp_path / "snap.json"
    today = dt.date(2026, 5, 12)

    captured: dict = {}

    def _capture_render(report) -> str:
        # Snapshot the report state at render time
        captured["report"] = report
        # Delegate to the real renderer so downstream works
        from src.store_metrics.digest import render_digest as _real
        return _real(report)

    with patch.object(
        cli.hypothesis, "generate", return_value=["frozen-safe insight"],
    ), patch("src.store_metrics.cli.render_digest", side_effect=_capture_render), \
         patch.object(cli, "send_to_planner", return_value=True):
        cli.main(today=today, snapshots_path=snap_path)

    report = captured["report"]
    # Hypotheses field populated (via dataclasses.replace — frozen-safe)
    assert report.hypotheses == ["frozen-safe insight"]
    # Confirm the dataclass is still frozen (sanity — replace produced a new
    # frozen instance, not a mutable copy).
    import dataclasses as _dc
    assert _dc.is_dataclass(report)
    with pytest.raises(_dc.FrozenInstanceError):
        report.hypotheses = ["should not mutate"]   # type: ignore[misc]


def test_main_hypothesis_injection_runs_before_render_digest(tmp_path, monkeypatch):
    """Ordering: hypothesis.generate is called BEFORE render_digest.

    This guarantees insights actually land in the rendered output (vs. being
    computed and discarded after the digest is already produced).
    """
    _strip_default_env(monkeypatch)
    snap_path = tmp_path / "snap.json"
    today = dt.date(2026, 5, 12)

    call_log: list[str] = []

    def _gen(*args, **kwargs):
        call_log.append("hypothesis.generate")
        return ["ordered insight"]

    def _render(report):
        call_log.append("render_digest")
        return "<rendered>"

    with patch.object(cli.hypothesis, "generate", side_effect=_gen), \
         patch("src.store_metrics.cli.render_digest", side_effect=_render), \
         patch.object(cli, "send_to_planner", return_value=True):
        cli.main(today=today, snapshots_path=snap_path)

    # Both called at least once; generate strictly before render_digest
    assert "hypothesis.generate" in call_log
    assert "render_digest" in call_log
    assert call_log.index("hypothesis.generate") < call_log.index("render_digest")


def test_main_spend_file_path_resolves_to_repo_root(tmp_path, monkeypatch):
    """SPEND_FILE constant resolves to marketing-v3/.metrics/api_spend.json
    (relative to the cli.py file location, not the test CWD)."""
    _strip_default_env(monkeypatch)
    snap_path = tmp_path / "snap.json"
    today = dt.date(2026, 5, 12)

    captured: dict = {}

    def _gen(report, *, spend_file: Path):
        captured["spend_file"] = spend_file
        return []

    with patch.object(cli.hypothesis, "generate", side_effect=_gen), \
         patch.object(cli, "send_to_planner", return_value=False):
        cli.main(today=today, snapshots_path=snap_path)

    sp: Path = captured["spend_file"]
    # Absolute path
    assert sp.is_absolute()
    # Ends with .metrics/api_spend.json
    assert sp.parts[-2:] == (".metrics", "api_spend.json")
    # Parent of parent is the marketing-v3 repo root (contains pyproject.toml)
    repo_root = sp.parent.parent
    assert (repo_root / "pyproject.toml").exists()
