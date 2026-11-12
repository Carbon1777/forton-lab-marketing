"""vk_link: ссылка уходит в wall.post вложением (карточка с og:image)."""
from pathlib import Path

import pytest

from src import vk_post


def _post(tmp_path: Path, extra: str) -> Path:
    p = tmp_path / "x.md"
    p.write_text(f"---\nchannels: [vk]\n{extra}---\n\nТекст поста\n", encoding="utf-8")
    return p


def test_link_goes_to_attachments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vk_post, "vk_call", lambda m, t, **p: calls.append(p) or {"post_id": 7})
    pid = vk_post.publish_one(_post(tmp_path, "vk_link: https://luceaweb.ru/matrica-sudby/\n"), "tok", "1")
    assert pid == 7
    assert calls[0]["attachments"] == "https://luceaweb.ru/matrica-sudby/"


def test_no_link_no_attachments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vk_post, "vk_call", lambda m, t, **p: calls.append(p) or {"post_id": 8})
    vk_post.publish_one(_post(tmp_path, ""), "tok", "1")
    assert "attachments" not in calls[0]


def test_bad_link_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(vk_post, "vk_call", lambda m, t, **p: {"post_id": 9})
    with pytest.raises(ValueError):
        vk_post.publish_one(_post(tmp_path, "vk_link: javascript:alert(1)\n"), "tok", "1")
