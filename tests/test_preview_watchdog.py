"""Unit tests for preview_watchdog — Phase 2.5 GH cron throttling guard."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest

from src import preview_watchdog


def _make_plan(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a minimal monthly_plan_YYYY-MM.md with given entries.

    Each entry dict: {date, slug, status[, channels, product, rubric]}.
    """
    plans = tmp_path / "plans"
    plans.mkdir(exist_ok=True)
    today = dt.date.today()
    path = plans / f"monthly_plan_{today.strftime('%Y-%m')}.md"
    out = ["---", f"month: {today.strftime('%Y-%m')}", "status: approved", "---", ""]
    for e in entries:
        out.append(f"## {e['date']}")
        out.append("")
        out.append("```yaml")
        out.append(f"slug: {e['slug']}")
        out.append(f"channels: {e.get('channels', ['tg'])}")
        out.append(f"product: {e.get('product', 'forton-lab')}")
        out.append(f"rubric: {e.get('rubric', 'philosophy')}")
        out.append(f"status: {e['status']}")
        out.append("```")
        out.append("")
        out.append(e.get("content", "Текст поста"))
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _mk_draft(tmp_path: Path, slug: str) -> Path:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    p = drafts / f"{slug}.md"
    post = frontmatter.Post(content="x", slug=slug)
    post.metadata["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    p.write_text(frontmatter.dumps(post), encoding="utf-8")
    return p


def test_watchdog_no_plan_silent_exit(tmp_path, monkeypatch):
    """Нет plan — silent exit 0."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    with patch.object(preview_watchdog, "_trigger_preview_bot") as mock_trig:
        rc = preview_watchdog.main()
    assert rc == 0
    mock_trig.assert_not_called()


def test_watchdog_no_today_entries_silent(tmp_path, monkeypatch):
    """Plan есть но без entry на сегодня — silent."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _make_plan(tmp_path, [{"date": yesterday, "slug": "old", "status": "published"}])
    with patch.object(preview_watchdog, "_trigger_preview_bot") as mock_trig:
        rc = preview_watchdog.main()
    assert rc == 0
    mock_trig.assert_not_called()


def test_watchdog_entry_already_pending_silent(tmp_path, monkeypatch):
    """Сегодняшняя запись draft + draft файл есть в кэше — silent."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    today_iso = dt.date.today().isoformat()
    _make_plan(tmp_path, [{"date": today_iso, "slug": "today-slug", "status": "draft"}])
    _mk_draft(tmp_path, "today-slug")
    with patch.object(preview_watchdog, "_trigger_preview_bot") as mock_trig:
        rc = preview_watchdog.main()
    assert rc == 0
    mock_trig.assert_not_called()


def test_watchdog_missing_preview_triggers_dispatch(tmp_path, monkeypatch):
    """draft без drafts/<slug>.md → trigger preview_bot + alert."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")
    today_iso = dt.date.today().isoformat()
    _make_plan(tmp_path, [{"date": today_iso, "slug": "missing-slug", "status": "draft"}])
    with patch.object(preview_watchdog, "_trigger_preview_bot",
                        return_value=(True, "")) as mock_trig, \
         patch("src.preview_watchdog.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        rc = preview_watchdog.main()
    assert rc == 0
    mock_trig.assert_called_once()
    # Alert was sent to TG
    mock_post.assert_called_once()
    sent = mock_post.call_args.kwargs["json"]
    assert "missing-slug" in sent["text"]
    assert "Watchdog" in sent["text"]


def test_watchdog_skips_published_and_skipped(tmp_path, monkeypatch):
    """Записи со статусами published/skipped/approved — игнорируются."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    today_iso = dt.date.today().isoformat()
    # 3 entries: один published, один skipped, один approved — все на сегодня
    _make_plan(tmp_path, [
        {"date": today_iso, "slug": "post-1", "status": "published"},
        {"date": today_iso, "slug": "post-2", "status": "skipped"},
        {"date": today_iso, "slug": "post-3", "status": "approved"},
    ])
    with patch.object(preview_watchdog, "_trigger_preview_bot") as mock_trig:
        rc = preview_watchdog.main()
    assert rc == 0
    mock_trig.assert_not_called()


def test_watchdog_dispatch_failure_still_alerts(tmp_path, monkeypatch):
    """dispatch упал (non-auth, напр. 500) → всё равно alert, чтобы юзер триггернул вручную."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")
    today_iso = dt.date.today().isoformat()
    _make_plan(tmp_path, [{"date": today_iso, "slug": "needy", "status": "draft"}])
    with patch.object(preview_watchdog, "_trigger_preview_bot",
                        return_value=(False, "HTTP 500: server error")), \
         patch("src.preview_watchdog.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        rc = preview_watchdog.main()
    assert rc == 0
    mock_post.assert_called_once()
    sent = mock_post.call_args.kwargs["json"]
    assert "dispatch failed" in sent["text"]
    # non-auth фейл — сохраняем формулировку про троттлинг
    assert "известное поведение" in sent["text"]
    assert "BOT_DISPATCH_PAT" not in sent["text"]


def test_watchdog_auth_failure_names_dead_pat(tmp_path, monkeypatch):
    """dispatch упал с 401 Bad credentials → alert прямо называет мёртвый PAT,
    БЕЗ убаюкивающего 'троттлинга', и даёт команду ротации."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(preview_watchdog, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "1:fake")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "-100123")
    monkeypatch.setenv("REPO_OWNER", "Carbon1777")
    monkeypatch.setenv("REPO_NAME", "forton-lab-marketing")
    today_iso = dt.date.today().isoformat()
    _make_plan(tmp_path, [{"date": today_iso, "slug": "needy", "status": "draft"}])
    err = 'HTTP 401: {"message":"Bad credentials","status":"401"}'
    with patch.object(preview_watchdog, "_trigger_preview_bot",
                        return_value=(False, err)), \
         patch("src.preview_watchdog.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        rc = preview_watchdog.main()
    assert rc == 0
    text = mock_post.call_args.kwargs["json"]["text"]
    assert "BOT_DISPATCH_PAT" in text
    assert "gh secret set BOT_DISPATCH_PAT" in text
    assert "Carbon1777/forton-lab-marketing" in text
    # на auth-фейле НЕ должно быть успокаивающего "это известное поведение / не баг"
    assert "известное поведение" not in text
    assert "восстанавливает" not in text


@pytest.mark.parametrize("err,expected", [
    ('HTTP 401: {"message":"Bad credentials"}', True),
    ("HTTP 403: Forbidden", True),
    ("something BAD CREDENTIALS here", True),
    ("HTTP 500: server error", False),
    ("ConnectionError: timed out", False),
    ("missing BOT_DISPATCH_PAT/REPO_OWNER/REPO_NAME", False),
    ("", False),
])
def test_is_auth_failure(err, expected):
    assert preview_watchdog._is_auth_failure(err) is expected


def test_trigger_preview_bot_missing_env(monkeypatch):
    """Без PAT/REPO_* — returns (False, error_msg)."""
    monkeypatch.delenv("BOT_DISPATCH_PAT", raising=False)
    monkeypatch.delenv("REPO_OWNER", raising=False)
    monkeypatch.delenv("REPO_NAME", raising=False)
    ok, err = preview_watchdog._trigger_preview_bot()
    assert ok is False
    assert "missing" in err.lower()


def test_trigger_preview_bot_http_success(monkeypatch):
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    monkeypatch.setenv("REPO_OWNER", "owner")
    monkeypatch.setenv("REPO_NAME", "repo")
    with patch("src.preview_watchdog.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=204, text="")
        ok, err = preview_watchdog._trigger_preview_bot()
    assert ok is True
    assert err == ""
    url = mock_post.call_args.args[0]
    assert "/owner/repo/actions/workflows/preview_bot.yml/dispatches" in url
