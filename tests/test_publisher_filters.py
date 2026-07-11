"""Phase 2 — _should_publish channel filter tests for all 3 publishers.

Combined into one file to avoid bootstrap of 3 separate test_<publisher>.py
modules (existing publishers don't have test files; that's a Phase 1 gap not
worth filling at this point — only the new Phase 2 filter is tested here).
"""
from __future__ import annotations

import frontmatter

from src.tg_post import _should_publish as tg_should
from src.vk_post import _should_publish as vk_should
from src.youtube_post import _should_publish as yt_should


# ===================================================================
# Backward-compat: post без channels → publish (Phase 1 legacy)
# ===================================================================

def test_tg_should_publish_backward_compat_no_channels():
    post = frontmatter.Post(content="x")
    assert tg_should(post, "tg") is True


def test_vk_should_publish_backward_compat_no_channels():
    post = frontmatter.Post(content="x")
    assert vk_should(post, "vk") is True


def test_yt_should_publish_backward_compat_no_channels():
    post = frontmatter.Post(content="x")
    assert yt_should(post, "yt") is True


def test_should_publish_empty_channels_list_treated_as_legacy():
    """channels: [] (пустой список) — backward-compat → publish."""
    post = frontmatter.Post(content="x", channels=[])
    assert tg_should(post, "tg") is True
    assert vk_should(post, "vk") is True
    assert yt_should(post, "yt") is True


# ===================================================================
# Filter logic: channel в списке → True; channel НЕ в списке → False
# ===================================================================

def test_tg_should_publish_filters_excluded_channel():
    """channels: [vk, yt] → tg_should → False."""
    post = frontmatter.Post(content="x", channels=["vk", "yt"])
    assert tg_should(post, "tg") is False


def test_vk_should_publish_filters_excluded_channel():
    """channels: [tg, yt] → vk_should → False."""
    post = frontmatter.Post(content="x", channels=["tg", "yt"])
    assert vk_should(post, "vk") is False


def test_yt_should_publish_filters_excluded_channel():
    """channels: [tg, vk] → yt_should → False."""
    post = frontmatter.Post(content="x", channels=["tg", "vk"])
    assert yt_should(post, "yt") is False


def test_should_publish_includes_listed_channel():
    """channels: [tg, vk, yt] → все 3 publishers True."""
    post = frontmatter.Post(content="x", channels=["tg", "vk", "yt"])
    assert tg_should(post, "tg") is True
    assert vk_should(post, "vk") is True
    assert yt_should(post, "yt") is True


def test_should_publish_dzen_via_tg_implicit():
    """Дзен — через TG cross-post; не отдельный publisher.

    Если channels: [tg, dzen] — TG публикуется (dzen наследуется), VK/YT skip.
    """
    post = frontmatter.Post(content="x", channels=["tg", "dzen"])
    assert tg_should(post, "tg") is True
    assert vk_should(post, "vk") is False
    assert yt_should(post, "yt") is False


# ===================================================================
# tg_post _move_to_published — для skip-with-move pattern
# ===================================================================

def test_tg_move_to_published_moves_file_with_date_prefix(tmp_path, monkeypatch):
    """skip-TG path должен всё равно переместить файл в published/ для VK/YT downstream."""
    import datetime as dt
    from src import tg_post

    # Patch REPO_ROOT to tmp_path-based location
    queue = tmp_path / "queue"; queue.mkdir()
    published = tmp_path / "published"
    monkeypatch.setattr(tg_post, "PUBLISHED_DIR", published)

    src = queue / "centry-test.md"
    src.write_text("---\nslug: x\n---\nbody", encoding="utf-8")

    new_path = tg_post._move_to_published(src)
    today = dt.date.today().isoformat()
    assert new_path.name == f"{today}-centry-test.md"
    assert new_path.exists()
    assert not src.exists()


def test_tg_move_to_published_keeps_existing_date_prefix(tmp_path, monkeypatch):
    """Если файл уже с date-prefix — имя не дублируется."""
    import datetime as dt
    from src import tg_post

    queue = tmp_path / "queue"; queue.mkdir()
    published = tmp_path / "published"
    monkeypatch.setattr(tg_post, "PUBLISHED_DIR", published)

    today = dt.date.today().isoformat()
    src = queue / f"{today}-pre-named.md"
    src.write_text("---\nslug: x\n---\nbody", encoding="utf-8")
    new_path = tg_post._move_to_published(src)
    assert new_path.name == f"{today}-pre-named.md"


# ===================================================================
# YouTube — strip_html: тело поста с TG-HTML не должно ронять
# videos.insert с invalidDescription/invalidTitle (символы < >).
# ===================================================================

def test_yt_strip_html_flattens_anchor_to_plain_text():
    """<a href="URL">текст</a> → «текст: URL», без символов < >."""
    from src.youtube_post import strip_html

    out = strip_html('Unia — <a href="https://uniaweb.ru/">ссылка</a>')
    assert out == "Unia — ссылка: https://uniaweb.ru/"
    assert "<" not in out and ">" not in out


def test_yt_derive_description_has_no_angle_brackets():
    """Регресс исходного бага (unia-jul11-dvoe): HTML-ссылка в body."""
    from pathlib import Path

    from src.youtube_post import derive_description, derive_title

    post = frontmatter.Post(
        content='Текст.\n\nUnia — <a href="https://uniaweb.ru/">ссылка</a>'
    )
    desc = derive_description(post)
    assert "<" not in desc and ">" not in desc
    assert "https://uniaweb.ru/" in desc

    # Заголовок из metadata с HTML тоже санируется (invalidTitle).
    titled = frontmatter.Post(content="x", youtube_title="<b>Заголовок</b>")
    title = derive_title(titled, Path("x.md"))
    assert title == "Заголовок"


def test_yt_strip_html_drops_other_tags_and_decodes_entities():
    """Прочие теги снимаются, сущности декодируются, брекеты не остаются."""
    from src.youtube_post import strip_html

    out = strip_html("<b>Жир</b> и Q&amp;A и &lt;код&gt;")
    assert "<" not in out and ">" not in out
    assert "Жир" in out and "Q&A" in out and "код" in out
