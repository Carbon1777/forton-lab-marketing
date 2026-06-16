"""Resilience tests for tg_post: a transient Telegram send error (HTTP 5xx /
timeout / connection drop) must NOT cascade-fail the publish pipeline.

Regression guard for incident 2026-06-16 (publish run 27608050178): a 504
Gateway Timeout on sendVideo (the video had actually been delivered) raised →
main() returned 1 → the GH Actions "publish" step exited 1 → VK, YouTube, the
queue→published commit, and the report nudge were all SKIPPED.

Fix contract: a transient send error → assume probable delivery, move the file
to published/ so VK/YouTube/commit/report downstream still run, flag
``tg_delivery: uncertain`` for manual TG verification, and do NOT count it as a
failure. A genuine 4xx (incl. 429) stays a hard failure (today's behavior).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import frontmatter
import pytest
import requests

from src import tg_post


def _resp_raising(status_code: int):
    """Mocked Response whose raise_for_status() raises HTTPError carrying
    response.status_code — mimics requests on a 4xx/5xx reply."""
    resp = MagicMock()
    err = requests.HTTPError(f"{status_code} Server Error")
    err.response = MagicMock(status_code=status_code)
    resp.raise_for_status.side_effect = err
    return resp


def _ok_resp():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"ok": True, "result": {"message_id": 123}}
    return resp


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Point tg_post's REPO_ROOT/QUEUE_DIR/PUBLISHED_DIR at a temp tree and stub
    the ffprobe/ffmpeg helpers (no external binaries in tests)."""
    queue = tmp_path / "queue"
    queue.mkdir()
    published = tmp_path / "published"
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(tg_post, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(tg_post, "QUEUE_DIR", queue)
    monkeypatch.setattr(tg_post, "PUBLISHED_DIR", published)
    monkeypatch.setattr(tg_post, "_probe_video", lambda p: None)
    monkeypatch.setattr(tg_post, "_make_thumbnail", lambda v, d: None)
    (assets / "v.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fakevideo")
    return {"queue": queue, "published": published, "assets": assets}


def _write_video_post(queue, name="lucea-test.md"):
    p = queue / name
    p.write_text(
        "---\nslug: lucea-test\nvideo: assets/v.mp4\nchannels: [tg, vk, yt]\n---\n"
        "Тело поста.\n",
        encoding="utf-8",
    )
    return p


def test_504_on_sendvideo_is_treated_as_delivered(repo, monkeypatch):
    monkeypatch.setattr("src.tg_post.requests.post",
                        MagicMock(return_value=_resp_raising(504)))
    src = _write_video_post(repo["queue"])

    new_path = tg_post.publish_one(src, "tok", "@chan")

    # moved out of queue into published/ so VK/YT/commit downstream still run
    assert not src.exists()
    assert new_path.parent == repo["published"]
    assert new_path.exists()
    post = frontmatter.load(new_path)
    assert post.metadata.get("tg_delivery") == "uncertain"
    assert "504" in str(post.metadata.get("tg_uncertain_reason", ""))


def test_timeout_on_send_is_treated_as_delivered(repo, monkeypatch):
    monkeypatch.setattr("src.tg_post.requests.post",
                        MagicMock(side_effect=requests.exceptions.Timeout("read timed out")))
    src = _write_video_post(repo["queue"])

    new_path = tg_post.publish_one(src, "tok", "@chan")

    assert not src.exists()
    assert new_path.exists()
    assert frontmatter.load(new_path).metadata.get("tg_delivery") == "uncertain"


def test_400_on_sendvideo_is_hard_failure(repo, monkeypatch):
    monkeypatch.setattr("src.tg_post.requests.post",
                        MagicMock(return_value=_resp_raising(400)))
    src = _write_video_post(repo["queue"])

    with pytest.raises(requests.HTTPError):
        tg_post.publish_one(src, "tok", "@chan")

    # hard failure: file stays in queue for retry, nothing moved to published/
    assert src.exists()
    assert not repo["published"].exists() or not list(repo["published"].glob("*.md"))


def test_happy_path_moves_without_uncertain_marker(repo, monkeypatch):
    monkeypatch.setattr("src.tg_post.requests.post",
                        MagicMock(return_value=_ok_resp()))
    src = _write_video_post(repo["queue"])

    new_path = tg_post.publish_one(src, "tok", "@chan")

    assert not src.exists()
    assert new_path.exists()
    assert "tg_delivery" not in frontmatter.load(new_path).metadata


def test_main_504_exits_zero(repo, monkeypatch):
    """main() returns 0 on a transient send error → pipeline continues."""
    monkeypatch.setattr("src.tg_post.requests.post",
                        MagicMock(return_value=_resp_raising(504)))
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHANNEL_ID", "@chan")
    _write_video_post(repo["queue"])

    assert tg_post.main() == 0


def test_main_400_exits_one(repo, monkeypatch):
    """main() returns 1 on a genuine hard error → today's behavior preserved."""
    monkeypatch.setattr("src.tg_post.requests.post",
                        MagicMock(return_value=_resp_raising(400)))
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHANNEL_ID", "@chan")
    _write_video_post(repo["queue"])

    assert tg_post.main() == 1
