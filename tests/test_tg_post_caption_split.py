"""Regression tests for tg_post caption splitting.

Guards incident 2026-08-04 (publish run 30894608324): approved post
`lapulya-aug4-stress` failed with `400 Bad Request` on sendPhoto because its
caption was 1044 chars — over Telegram's 1024 caption limit for
sendPhoto/sendVideo. `preview_bot.py` already splits a body >1024 into
text + media, but `tg_post.py` (the real publisher) only logged a WARN and
still sent the oversized caption → Telegram rejected it → whole run failed.

Fix contract: when body > TG_CAPTION_LIMIT on a photo/video post, send the
media with an EMPTY caption and the full body as a separate sendMessage. The
≤1024 path is unchanged (single captioned media). Preview and production must
never diverge on this again.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import frontmatter
import pytest

from src import tg_post


def _ok_resp():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"ok": True, "result": {"message_id": 123}}
    return resp


@pytest.fixture
def repo(tmp_path, monkeypatch):
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
    (assets / "img.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    (assets / "v.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fakevideo")
    return {"queue": queue, "published": published, "assets": assets}


def _write_post(queue, *, media_line: str, body: str, name="lapulya-test.md"):
    p = queue / name
    p.write_text(
        f"---\nslug: lapulya-test\n{media_line}\nchannels: [tg]\n---\n{body}\n",
        encoding="utf-8",
    )
    return p


def _classify_calls(mock_post):
    """Split recorded requests.post calls into (photo_or_video, text) by URL."""
    media_calls, text_calls = [], []
    for call in mock_post.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        if url.endswith("/sendPhoto") or url.endswith("/sendVideo"):
            media_calls.append(call)
        elif url.endswith("/sendMessage"):
            text_calls.append(call)
    return media_calls, text_calls


LONG_BODY = "Ы" * 1044   # > TG_CAPTION_LIMIT (1024), < TG_TEXT_LIMIT (4096)
SHORT_BODY = "Короткое тело поста."


def test_long_caption_photo_splits_into_media_plus_text(repo, monkeypatch):
    post = MagicMock(return_value=_ok_resp())
    monkeypatch.setattr("src.tg_post.requests.post", post)
    src = _write_post(repo["queue"], media_line="image: assets/img.png", body=LONG_BODY)

    new_path = tg_post.publish_one(src, "tok", "@chan")

    media_calls, text_calls = _classify_calls(post)
    # exactly one photo send + one follow-up text send
    assert len(media_calls) == 1
    assert len(text_calls) == 1
    # photo caption is empty (never the oversized body)
    photo_caption = media_calls[0].kwargs["data"]["caption"]
    assert photo_caption == ""
    # full body went out as the text message
    assert text_calls[0].kwargs["json"]["text"] == LONG_BODY
    # no send ever carried a caption over the limit
    for c in media_calls:
        assert len(c.kwargs["data"].get("caption", "")) <= tg_post.TG_CAPTION_LIMIT
    # published normally
    assert not src.exists()
    assert new_path.parent == repo["published"]


def test_long_caption_video_splits_into_media_plus_text(repo, monkeypatch):
    post = MagicMock(return_value=_ok_resp())
    monkeypatch.setattr("src.tg_post.requests.post", post)
    src = _write_post(repo["queue"], media_line="video: assets/v.mp4", body=LONG_BODY)

    tg_post.publish_one(src, "tok", "@chan")

    media_calls, text_calls = _classify_calls(post)
    assert len(media_calls) == 1
    assert len(text_calls) == 1
    assert media_calls[0].kwargs["data"]["caption"] == ""
    assert text_calls[0].kwargs["json"]["text"] == LONG_BODY


def test_short_caption_photo_stays_single_send(repo, monkeypatch):
    """≤1024 body: unchanged behavior — one captioned photo, no split text."""
    post = MagicMock(return_value=_ok_resp())
    monkeypatch.setattr("src.tg_post.requests.post", post)
    src = _write_post(repo["queue"], media_line="image: assets/img.png", body=SHORT_BODY)

    tg_post.publish_one(src, "tok", "@chan")

    media_calls, text_calls = _classify_calls(post)
    assert len(media_calls) == 1
    assert len(text_calls) == 0
    assert media_calls[0].kwargs["data"]["caption"] == SHORT_BODY


def test_oversized_body_beyond_text_limit_still_hard_fails(repo, monkeypatch):
    """A body over the 4096 sendMessage limit is a real content problem — the
    split text send must surface the Telegram error, not silently truncate."""
    import requests as _requests

    def _raise_on_text(url, *a, **kw):
        if url.endswith("/sendMessage"):
            resp = MagicMock()
            err = _requests.HTTPError("400 Bad Request")
            err.response = MagicMock(status_code=400)
            resp.raise_for_status.side_effect = err
            return resp
        return _ok_resp()

    monkeypatch.setattr("src.tg_post.requests.post", MagicMock(side_effect=_raise_on_text))
    src = _write_post(repo["queue"], media_line="image: assets/img.png", body="Я" * 5000)

    with pytest.raises(_requests.HTTPError):
        tg_post.publish_one(src, "tok", "@chan")
