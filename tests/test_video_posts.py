"""Видео-посты: extras в плане, отложенная публикация, Meta, ручной пакет, CLI записи."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest

from src import dispatch_scheduled, manual_package, meta_post, preview_bot, video_plan_entry
from src.plan_reader import parse_plan_text

PLAN = """---
month: 2026-10
generated_at: 2026-10-02T00:00:00Z
status: approved
---

## 2026-10-30

```yaml
slug: v1
channels: [tg, ig]
product: listvia
verbatim: true
publish_at_msk: '20:00'
youtube_title: Заголовок для YT
media:
- path: assets/video/v1.mp4
  sha256: abc
  role: video
status: scheduled
```

Текст.

## 2026-11-02

```yaml
slug: v2
channels: [tg]
product: listvia
status: scheduled
```

Позже.
"""


def test_plan_entry_extras_roundtrip():
    plan = parse_plan_text(PLAN, Path("x.md"))
    e = plan.entries[0]
    assert e.extras == {"publish_at_msk": "20:00", "youtube_title": "Заголовок для YT"}
    assert plan.entries[1].extras == {}


def test_find_due_only_scheduled_not_future(tmp_path):
    (tmp_path / "monthly_plan_2026-10.md").write_text(PLAN, encoding="utf-8")
    due = dispatch_scheduled.find_due(tmp_path, dt.date(2026, 10, 30))
    assert [e.slug for e in due] == ["v1"]
    due_later = dispatch_scheduled.find_due(tmp_path, dt.date(2026, 11, 3))
    assert [e.slug for e in due_later] == ["v1", "v2"]   # просроченные тоже ретраятся


def test_main_dispatches_each_due(tmp_path, monkeypatch):
    (tmp_path / "monthly_plan_2026-10.md").write_text(PLAN, encoding="utf-8")
    monkeypatch.setattr(dispatch_scheduled, "PLANS_DIR", tmp_path)
    monkeypatch.setattr(dispatch_scheduled, "_today_msk", lambda: dt.date(2026, 10, 30))
    with patch.object(dispatch_scheduled, "dispatch_publish") as d:
        assert dispatch_scheduled.main() == 0
    d.assert_called_once_with("v1")


def test_before_msk_time():
    fake_now = dt.datetime(2026, 10, 30, 14, 0, tzinfo=dt.timezone.utc)  # 17:00 МСК

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(preview_bot.dt, "datetime", _DT):
        assert preview_bot._before_msk_time("20:00") is True
        assert preview_bot._before_msk_time("16:59") is False
        assert preview_bot._before_msk_time("мусор") is False


def test_classify_scheduled_is_terminal(tmp_path):
    plan = parse_plan_text(PLAN, Path("x.md"))
    assert preview_bot._classify_entry_state(plan.entries[0], tmp_path) == "scheduled"


# --------------------------------------------------------------------------- Meta

def _resp(json_data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.text = str(json_data)
    return r


def test_instagram_reel_flow(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 100)
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/IG/media"):
            assert kw["data"]["media_type"] == "REELS" and kw["data"]["upload_type"] == "resumable"
            return _resp({"id": "C1"})
        if "rupload" in url:
            assert kw["headers"]["file_size"] == "100"
            return _resp({"success": True})
        if url.endswith("/IG/media_publish"):
            assert kw["data"]["creation_id"] == "C1"
            return _resp({"id": "M1"})
        raise AssertionError(url)

    statuses = iter(["IN_PROGRESS", "FINISHED"])
    with patch.object(meta_post.requests, "post", side_effect=fake_post), \
         patch.object(meta_post.requests, "get",
                      side_effect=lambda *a, **k: _resp({"status_code": next(statuses)})):
        mid = meta_post.publish_instagram_reel(video, "cap", ig_user_id="IG", token="T",
                                               sleep=lambda s: None)
    assert mid == "M1" and len(calls) == 3


def test_instagram_error_status_raises(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with patch.object(meta_post.requests, "post", return_value=_resp({"id": "C1"})), \
         patch.object(meta_post.requests, "get", return_value=_resp({"status_code": "ERROR"})):
        with pytest.raises(meta_post.MetaAPIError):
            meta_post.publish_instagram_reel(video, "c", ig_user_id="IG", token="T",
                                             sleep=lambda s: None)


def test_facebook_reel_flow(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    phases = []

    def fake_post(url, **kw):
        if "rupload" in url or "upload.example" in url:
            return _resp({"success": True})
        phases.append(kw["data"]["upload_phase"])
        if kw["data"]["upload_phase"] == "start":
            return _resp({"video_id": "V9", "upload_url": "https://upload.example/x"})
        assert kw["data"]["video_state"] == "PUBLISHED"
        return _resp({"success": True})

    with patch.object(meta_post.requests, "post", side_effect=fake_post):
        vid = meta_post.publish_facebook_reel(video, "d", page_id="P", token="T")
    assert vid == "V9" and phases == ["start", "finish"]


def test_meta_error_message_has_no_token():
    r = _resp({"error": {"code": 190, "message": "bad"}}, 400)
    with pytest.raises(meta_post.MetaAPIError) as ei:
        meta_post._check(r, "x")
    assert "190" in str(ei.value) and "token" not in str(ei.value).lower()


def test_meta_requires_explicit_channels(tmp_path, monkeypatch):
    pub = tmp_path / "published"
    pub.mkdir()
    for name, ch in (("a.md", ["tg"]), ("b.md", ["tg", "ig"]), ("c.md", None)):
        post = frontmatter.Post("t", video="assets/video/x.mp4")
        if ch:
            post.metadata["channels"] = ch
        (pub / name).write_text(frontmatter.dumps(post), encoding="utf-8")
    monkeypatch.setattr(meta_post, "PUBLISHED_DIR", pub)
    assert [p.name for p in meta_post.find_pending_files()] == ["b.md"]


def test_meta_main_skips_without_token(tmp_path, monkeypatch, capsys):
    pub = tmp_path / "published"
    pub.mkdir()
    post = frontmatter.Post("t", video="v.mp4", channels=["ig"])
    (pub / "a.md").write_text(frontmatter.dumps(post), encoding="utf-8")
    monkeypatch.setattr(meta_post, "PUBLISHED_DIR", pub)
    monkeypatch.delenv("META_PAGE_TOKEN", raising=False)
    assert meta_post.main() == 0
    assert "пропускаем" in capsys.readouterr().out


def test_strip_html_links():
    assert meta_post.strip_html('Тут <a href="https://x.ru/">ссылка</a> <b>да</b>') == "Тут ссылка https://x.ru/ да"


# --------------------------------------------------------------------- manual pkg

def test_manual_package_targets_and_pending(tmp_path, monkeypatch):
    pub = tmp_path / "published"
    pub.mkdir()
    for name, ch, sent in (("a.md", ["tg"], False), ("b.md", ["tg", "vk"], False),
                           ("c.md", ["rutube"], True)):
        post = frontmatter.Post("t", video="v.mp4", channels=ch)
        if sent:
            post.metadata["manual_pkg_sent"] = "x"
        (pub / name).write_text(frontmatter.dumps(post), encoding="utf-8")
    monkeypatch.setattr(manual_package, "PUBLISHED_DIR", pub)
    assert [p.name for p in manual_package.find_pending_files()] == ["b.md"]


def test_manual_package_sends_and_marks(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    monkeypatch.setattr(manual_package, "REPO_ROOT", tmp_path)
    post_path = tmp_path / "p.md"
    post_path.write_text(frontmatter.dumps(
        frontmatter.Post("Привет <b>мир</b>", video="v.mp4", channels=["vk", "rutube"])), encoding="utf-8")
    sent = []
    monkeypatch.setattr(manual_package, "_api", lambda token, method, **kw: sent.append(method) or {"ok": True})
    manual_package.send_package(post_path, "T", "1")
    assert sent == ["sendVideo", "sendMessage"]
    assert frontmatter.load(post_path).metadata["manual_pkg_sent"]


# ------------------------------------------------------------------ plan entry CLI

DESCR = """ЛИСТВИЯ
==================================================
01 · a.mp4
==================================================
Текст один
listviaweb.ru

#тег

==================================================
02 · b.mp4
==================================================
Текст два

==================================================
ПРИМЕЧАНИЕ
не брать
"""


def test_read_description_block(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text(DESCR, encoding="utf-8")
    assert video_plan_entry.read_description_block(f, 1).startswith("Текст один")
    assert video_plan_entry.read_description_block(f, 2) == "Текст два"
    with pytest.raises(ValueError):
        video_plan_entry.read_description_block(f, 9)


def test_build_entry_text_parses_back():
    txt = video_plan_entry.build_entry_text(
        date=dt.date(2026, 10, 30), slug="s1", channels=["tg", "yt"], product="listvia",
        rubric="promo_video", video_rel="assets/video/s1.mp4", sha256="a" * 64, body="Тело",
        publish_at="20:00", youtube_title="YT", youtube_tags=["a", "b"],
    )
    plan = parse_plan_text("---\nmonth: 2026-10\n---\n" + txt, Path("x"))
    e = plan.entries[0]
    assert (e.slug, e.verbatim, e.status) == ("s1", True, "draft")
    assert e.extras["publish_at_msk"] == "20:00" and e.extras["youtube_tags"] == ["a", "b"]
    assert e.media[0].role == "video" and e.content == "Тело"
