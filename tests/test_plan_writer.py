"""Unit tests for plan_writer — GH API mutation + frontmatter rewrite + sha8.

All HTTP I/O mocked via pytest-mock (mocker.patch). No real network calls.
Spec for Phase 1.5 Plan 02 — covers D-1.5-02 (approve via GH API), D-1.5-03
(reject via workflow_dispatch), T-1.5-03 (optimistic concurrency on PUT 409),
T-1.5-04 (sha8 anti-replay), T-1.5-05 (no PAT in logs).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ============================================================
# fetch_file
# ============================================================

def test_fetch_file_decodes_base64(mocker):
    """GET /contents returns base64 content + blob sha; we decode UTF-8."""
    from src.plan_writer import fetch_file
    fake = MagicMock(status_code=200)
    fake.json.return_value = {
        "content": base64.b64encode(b"---\nmonth: 2026-06\nstatus: draft\n---\nbody").decode(),
        "sha": "blob_xyz",
    }
    mocker.patch("src.plan_writer.requests.get", return_value=fake)
    text, sha = fetch_file("pat", "owner", "repo", "plans/monthly_plan_2026-06.md")
    assert "month: 2026-06" in text
    assert sha == "blob_xyz"


def test_fetch_file_404_raises(mocker):
    """Non-200 → GitHubAPIError carrying status code."""
    from src.plan_writer import fetch_file, GitHubAPIError
    fake = MagicMock(status_code=404, text='{"message":"not found"}')
    mocker.patch("src.plan_writer.requests.get", return_value=fake)
    with pytest.raises(GitHubAPIError, match="404"):
        fetch_file("pat", "o", "r", "p")


def test_fetch_file_uses_correct_headers(mocker):
    """Bearer auth + GH API version header sent on every request."""
    from src.plan_writer import fetch_file
    fake = MagicMock(status_code=200)
    fake.json.return_value = {"content": base64.b64encode(b"x").decode(), "sha": "s"}
    spy = mocker.patch("src.plan_writer.requests.get", return_value=fake)
    fetch_file("my_pat", "o", "r", "p")
    called_kwargs = spy.call_args.kwargs
    headers = called_kwargs["headers"]
    assert headers["Authorization"] == "Bearer my_pat"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["Accept"] == "application/vnd.github+json"


# ============================================================
# commit_file
# ============================================================

def test_commit_file_returns_commit_sha(mocker):
    """200 PUT response contains commit.sha; we return it."""
    from src.plan_writer import commit_file
    fake = MagicMock(status_code=201)
    fake.json.return_value = {"commit": {"sha": "abc1234deadbeef"}}
    mocker.patch("src.plan_writer.requests.put", return_value=fake)
    sha = commit_file("pat", "o", "r", "p", "new content", "blob_sha", "msg")
    assert sha == "abc1234deadbeef"


def test_commit_file_409_raises(mocker):
    """409 Conflict (file changed since GET) → GitHubAPIError matching 409.

    T-1.5-03 mitigation — bot catches this and tells user 'план изменился'.
    """
    from src.plan_writer import commit_file, GitHubAPIError
    fake = MagicMock(status_code=409, text='{"message":"sha mismatch"}')
    mocker.patch("src.plan_writer.requests.put", return_value=fake)
    with pytest.raises(GitHubAPIError, match="409"):
        commit_file("pat", "o", "r", "p", "content", "stale_sha", "msg")


def test_commit_file_500_raises(mocker):
    """Other non-2xx (e.g. 500) → GitHubAPIError."""
    from src.plan_writer import commit_file, GitHubAPIError
    fake = MagicMock(status_code=500, text="internal")
    mocker.patch("src.plan_writer.requests.put", return_value=fake)
    with pytest.raises(GitHubAPIError, match="500"):
        commit_file("pat", "o", "r", "p", "c", "s", "m")


def test_commit_file_body_contains_base64_content_and_sha(mocker):
    """PUT body must include `content` (base64), `sha` (blob), `branch`, `message`."""
    from src.plan_writer import commit_file
    fake = MagicMock(status_code=200)
    fake.json.return_value = {"commit": {"sha": "x"}}
    spy = mocker.patch("src.plan_writer.requests.put", return_value=fake)
    commit_file("pat", "o", "r", "p", "hello", "blob_abc", "msg", branch="main")
    body = spy.call_args.kwargs["json"]
    assert body["sha"] == "blob_abc"
    assert body["branch"] == "main"
    assert body["message"] == "msg"
    assert base64.b64decode(body["content"]).decode() == "hello"


# ============================================================
# mutate_frontmatter_to_approved
# ============================================================

def test_mutate_adds_approve_fields():
    """Top-level frontmatter gets status=approved + approved_at + approved_by."""
    from src.plan_writer import mutate_frontmatter_to_approved
    plan_text = (
        "---\n"
        "month: 2026-06\n"
        "generated_at: 2026-06-01T07:23:14Z\n"
        "model: claude-sonnet-4-5\n"
        "status: draft\n"
        "_schema_version: 1\n"
        "---\n"
        "# План\n\n## 2026-06-01\n\n```yaml\nslug: x\n```\n\nbody1"
    )
    new = mutate_frontmatter_to_approved(plan_text, approver="forton-via-tg-bot")
    import frontmatter
    post = frontmatter.loads(new)
    assert post.metadata["status"] == "approved"
    assert post.metadata["approved_by"] == "forton-via-tg-bot"
    assert "approved_at" in post.metadata
    assert post.metadata["approved_at"].endswith("+00:00") or "T" in str(post.metadata["approved_at"])
    # existing fields preserved
    assert post.metadata["month"] == "2026-06"
    assert post.metadata["model"] == "claude-sonnet-4-5"
    assert post.metadata["_schema_version"] == 1
    # body untouched
    assert "## 2026-06-01" in post.content
    assert "slug: x" in post.content


def test_mutate_preserves_per_day_yaml_blocks():
    """Per-day fenced ```yaml blocks must NOT be modified."""
    from src.plan_writer import mutate_frontmatter_to_approved
    plan_text = (
        "---\nmonth: 2026-06\nstatus: draft\n---\n"
        "## 2026-06-01\n\n```yaml\nstatus: draft\nslug: x\n```\n"
    )
    new = mutate_frontmatter_to_approved(plan_text, "tg-bot")
    assert "```yaml\nstatus: draft\nslug: x\n```" in new


def test_mutate_default_approver():
    """approver param has a sane default ('forton-via-tg-bot')."""
    from src.plan_writer import mutate_frontmatter_to_approved
    plan_text = "---\nmonth: 2026-06\nstatus: draft\n---\nbody"
    new = mutate_frontmatter_to_approved(plan_text)
    import frontmatter
    post = frontmatter.loads(new)
    assert post.metadata["approved_by"] == "forton-via-tg-bot"


# ============================================================
# approve_plan (e2e mocked)
# ============================================================

def test_approve_plan_e2e_mocked(mocker, tmp_path, monkeypatch):
    """approve_plan reads PAT from env, calls fetch+mutate+commit, returns commit sha."""
    from src.plan_writer import approve_plan
    plan_text = "---\nmonth: 2026-06\nstatus: draft\n---\nbody"
    plan_path = tmp_path / "plans" / "monthly_plan_2026-06.md"
    plan_path.parent.mkdir()
    plan_path.write_text(plan_text)

    monkeypatch.setenv("BOT_DISPATCH_PAT", "test_pat")
    monkeypatch.setenv("REPO_OWNER", "Carbon1777")
    monkeypatch.setenv("REPO_NAME", "forton-lab-marketing")

    mocker.patch("src.plan_writer.fetch_file", return_value=(plan_text, "blob_abc"))
    mock_commit = mocker.patch("src.plan_writer.commit_file", return_value="commit_xyz")

    sha = approve_plan(plan_path, repo_root=tmp_path, month="2026-06",
                       approver="forton-via-tg-bot")
    assert sha == "commit_xyz"
    args = mock_commit.call_args.args
    kwargs = mock_commit.call_args.kwargs
    assert args[0] == "test_pat"
    assert args[1] == "Carbon1777"
    assert args[2] == "forton-lab-marketing"
    assert args[3] == "plans/monthly_plan_2026-06.md"
    new_text = args[4]
    assert "status: approved" in new_text
    assert "approved_by: forton-via-tg-bot" in new_text
    assert args[5] == "blob_abc"
    # commit message format — accepts positional or keyword
    msg = kwargs.get("message") or (args[6] if len(args) > 6 else "")
    assert "approved by Forton via TG bot for 2026-06" in msg


def test_approve_plan_uses_default_owner_repo(mocker, tmp_path, monkeypatch):
    """Without REPO_OWNER/REPO_NAME env, defaults to Carbon1777/forton-lab-marketing."""
    from src.plan_writer import approve_plan
    plan_text = "---\nmonth: 2026-06\nstatus: draft\n---\nbody"
    plan_path = tmp_path / "plans" / "monthly_plan_2026-06.md"
    plan_path.parent.mkdir()
    plan_path.write_text(plan_text)

    monkeypatch.setenv("BOT_DISPATCH_PAT", "test_pat")
    monkeypatch.delenv("REPO_OWNER", raising=False)
    monkeypatch.delenv("REPO_NAME", raising=False)

    mocker.patch("src.plan_writer.fetch_file", return_value=(plan_text, "blob_abc"))
    mock_commit = mocker.patch("src.plan_writer.commit_file", return_value="commit_xyz")

    approve_plan(plan_path, repo_root=tmp_path, month="2026-06")
    args = mock_commit.call_args.args
    assert args[1] == "Carbon1777"
    assert args[2] == "forton-lab-marketing"


def test_approve_plan_missing_pat_raises(tmp_path, monkeypatch):
    """Missing BOT_DISPATCH_PAT → KeyError surfaced (fail-fast)."""
    from src.plan_writer import approve_plan
    plan_path = tmp_path / "plans" / "monthly_plan_2026-06.md"
    plan_path.parent.mkdir()
    plan_path.write_text("---\nstatus: draft\n---\nbody")
    monkeypatch.delenv("BOT_DISPATCH_PAT", raising=False)
    with pytest.raises(KeyError):
        approve_plan(plan_path, repo_root=tmp_path, month="2026-06")


# ============================================================
# dispatch_regenerate
# ============================================================

def test_dispatch_posts_204(mocker):
    """204 No Content (success); function returns None."""
    from src.plan_writer import dispatch_regenerate
    fake = MagicMock(status_code=204, text="")
    spy = mocker.patch("src.plan_writer.requests.post", return_value=fake)
    result = dispatch_regenerate("pat", "Carbon1777", "forton-lab-marketing",
                                  workflow="monthly_plan.yml", ref="main",
                                  inputs={"month": "2026-06", "force_regenerate": "true"})
    assert result is None
    body = spy.call_args.kwargs["json"]
    assert body["ref"] == "main"
    assert body["inputs"]["month"] == "2026-06"
    assert body["inputs"]["force_regenerate"] == "true"


def test_dispatch_url_contains_workflow_path(mocker):
    """URL points to /actions/workflows/{workflow}/dispatches."""
    from src.plan_writer import dispatch_regenerate
    fake = MagicMock(status_code=204, text="")
    spy = mocker.patch("src.plan_writer.requests.post", return_value=fake)
    dispatch_regenerate("pat", "o", "r", workflow="monthly_plan.yml", inputs={})
    url = spy.call_args.args[0]
    assert "/actions/workflows/monthly_plan.yml/dispatches" in url


def test_dispatch_non_204_raises(mocker):
    """Non-204 (401/403/422) → GitHubAPIError."""
    from src.plan_writer import dispatch_regenerate, GitHubAPIError
    fake = MagicMock(status_code=403, text='{"message":"forbidden"}')
    mocker.patch("src.plan_writer.requests.post", return_value=fake)
    with pytest.raises(GitHubAPIError, match="403"):
        dispatch_regenerate("pat", "o", "r", inputs={})


# ============================================================
# plan_sha8 — anti-replay foundation (T-1.5-04)
# ============================================================

def test_plan_sha8_deterministic(tmp_path):
    """Same bytes → same sha8. Different bytes → different sha8."""
    from src.plan_writer import plan_sha8
    f1 = tmp_path / "a.md"
    f1.write_bytes(b"hello world")
    f2 = tmp_path / "b.md"
    f2.write_bytes(b"hello world")
    assert plan_sha8(f1) == plan_sha8(f2)
    f3 = tmp_path / "c.md"
    f3.write_bytes(b"hello world!")
    assert plan_sha8(f3) != plan_sha8(f1)


def test_plan_sha8_format(tmp_path):
    """sha8 is exactly 8 lowercase hex chars."""
    from src.plan_writer import plan_sha8
    f = tmp_path / "x.md"
    f.write_bytes(b"some content")
    s = plan_sha8(f)
    assert re.fullmatch(r"[0-9a-f]{8}", s)


def test_plan_sha8_matches_known_sha256(tmp_path):
    """plan_sha8(file) == hashlib.sha256(bytes).hexdigest()[:8]."""
    from src.plan_writer import plan_sha8
    f = tmp_path / "x.md"
    data = b"the quick brown fox"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()[:8]
    assert plan_sha8(f) == expected


# ============================================================
# read_regen_count / read_regen_limit re-exports
# ============================================================

def test_v1_tracker_compat(tmp_path):
    """Re-exported from spend_tracker_v2 — v1 schema → 0."""
    from src.plan_writer import read_regen_count, read_regen_limit
    spend = tmp_path / "api_spend.json"
    spend.write_text(json.dumps({
        "_schema_version": 1,
        "_updated": "2026-05-01T00:00:00Z",
        "2026-05": {"input_tokens": 100, "output_tokens": 50, "usd": 0.001, "calls": 1}
    }))
    assert read_regen_count(spend, "2026-05") == 0
    assert read_regen_limit(spend) == 3


def test_reexports_are_same_object():
    """plan_writer.read_regen_count is literally spend_tracker_v2.read_regen_count."""
    from src import plan_writer
    from src import spend_tracker_v2
    assert plan_writer.read_regen_count is spend_tracker_v2.read_regen_count
    assert plan_writer.read_regen_limit is spend_tracker_v2.read_regen_limit
    assert plan_writer.DEFAULT_REGEN_LIMIT == spend_tracker_v2.DEFAULT_REGEN_LIMIT


# ============================================================
# T-1.5-05 — no PAT leaks in error messages or logs
# ============================================================

def test_no_token_in_logs(mocker, caplog):
    """PAT must NEVER appear in log records or exception messages.

    Mitigates T-1.5-05-A (PAT in logs).
    """
    from src.plan_writer import fetch_file, commit_file, dispatch_regenerate, GitHubAPIError
    pat = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    pat2 = "github_pat_BBBBBBBBBBBBBBBBBBBBBBBB_CCCCCCCCCCCCCCCCCCCCCCCCC"

    # error path GET
    fake_get = MagicMock(status_code=500, text="internal")
    mocker.patch("src.plan_writer.requests.get", return_value=fake_get)
    with caplog.at_level(logging.DEBUG, logger="src.plan_writer"):
        try:
            fetch_file(pat, "o", "r", "p")
        except GitHubAPIError as e:
            assert pat not in str(e)

    # error path PUT
    fake_put = MagicMock(status_code=500, text="bad")
    mocker.patch("src.plan_writer.requests.put", return_value=fake_put)
    with caplog.at_level(logging.DEBUG, logger="src.plan_writer"):
        try:
            commit_file(pat2, "o", "r", "p", "c", "s", "m")
        except GitHubAPIError as e:
            assert pat2 not in str(e)

    # error path POST
    fake_post = MagicMock(status_code=403, text="nope")
    mocker.patch("src.plan_writer.requests.post", return_value=fake_post)
    with caplog.at_level(logging.DEBUG, logger="src.plan_writer"):
        try:
            dispatch_regenerate(pat, "o", "r", inputs={})
        except GitHubAPIError as e:
            assert pat not in str(e)

    # No log record contains the PAT either
    for record in caplog.records:
        assert pat not in record.getMessage()
        assert pat2 not in record.getMessage()
