"""max_post: выбор постов, загрузка медиа, повтор для видео, отметка max_post_id."""
from pathlib import Path

import frontmatter
import pytest

from src import max_post


def write(tmp_path: Path, name: str, meta: str, body: str = "Текст <b>поста</b>") -> Path:
    p = tmp_path / name
    p.write_text(f"---\n{meta}---\n\n{body}\n", encoding="utf-8")
    return p


# ---------- выбор ----------

@pytest.mark.parametrize("name,meta,expected", [
    ("2026-11-20-a.md", "channels: [tg, vk]\n", True),            # повторяет Telegram
    ("2026-11-20-b.md", "channels: [vk, max]\n", True),           # явно max
    ("2026-11-20-c.md", "channels: [vk, dzen]\n", False),         # ни tg, ни max
    ("2026-11-10-d.md", "channels: [tg]\n", False),               # раньше даты старта
    ("2026-11-20-e.md", "channels: [tg]\nmax_post_id: mid.1\n", False),  # уже в MAX
    ("2026-11-20-f.md", "channels: [tg]\nmax_skip: true\n", False),
    ("legacy.md", "channels: [tg]\n", False),                     # нет даты в имени
])
def test_should_publish(tmp_path, name, meta, expected):
    p = write(tmp_path, name, meta)
    assert max_post.should_publish(frontmatter.load(p), p, "2026-11-14") is expected


def test_find_pending_uses_published_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(max_post, "PUBLISHED_DIR", tmp_path)
    write(tmp_path, "2026-11-20-a.md", "channels: [tg]\n")
    write(tmp_path, "2026-11-01-old.md", "channels: [tg]\n")
    assert [p.name for p in max_post.find_pending("2026-11-14")] == ["2026-11-20-a.md"]


# ---------- публикация ----------

class FakeApi:
    """Подмена max_call: записывает вызовы и отдаёт заготовленные ответы."""

    def __init__(self, send_errors=0):
        self.calls = []
        self.send_errors = send_errors

    def __call__(self, method, path, token, *, params=None, json=None, files=None, url=None):
        self.calls.append((method, path or url, params, json))
        if path == "/uploads":
            if params["type"] == "image":
                return {"url": "https://iu.example/upload"}
            return {"url": "https://vu.example/upload", "token": "vtok"}
        if url == "https://iu.example/upload":
            return {"photos": {"12345": {"token": "itok"}}}
        if path == "/messages":
            if self.send_errors:
                self.send_errors -= 1
                raise max_post.MaxError('HTTP 400 {"code": "attachment.not.ready"}')
            return {"message": {"body": {"mid": "mid.42"}}}
        raise AssertionError(f"unexpected call {method} {path} {url}")


def test_text_post_and_mark(tmp_path, monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(max_post, "max_call", api)
    p = write(tmp_path, "2026-11-20-a.md", "channels: [tg]\n")
    assert max_post.publish_one(p, "tok", "-100") == "mid.42"
    method, path, params, body = api.calls[-1]
    assert (method, path, params) == ("POST", "/messages", {"chat_id": "-100"})
    assert body["format"] == "html" and body["text"] == "Текст <b>поста</b>"
    assert "attachments" not in body
    meta = frontmatter.load(p).metadata
    assert meta["max_post_id"] == "mid.42" and meta["max_posted_at"]


def test_image_post(tmp_path, monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(max_post, "max_call", api)
    monkeypatch.setattr(max_post, "REPO_ROOT", tmp_path)
    (tmp_path / "pic.png").write_bytes(b"png")
    p = write(tmp_path, "2026-11-20-a.md",
              "channels: [tg]\nimage: pic.png\nmax_button: {text: Рассчитать, url: 'https://luceaweb.ru/'}\n")
    max_post.publish_one(p, "tok", "-100")
    body = api.calls[-1][3]
    assert body["attachments"][0] == {"type": "image", "payload": {"token": "itok"}}
    assert body["attachments"][1]["payload"]["buttons"][0][0] == {
        "type": "link", "text": "Рассчитать", "url": "https://luceaweb.ru/"}


def test_video_retries_until_ready(tmp_path, monkeypatch):
    api = FakeApi(send_errors=2)
    monkeypatch.setattr(max_post, "max_call", api)
    monkeypatch.setattr(max_post, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(max_post, "upload_video", lambda token, path: "vtok")
    (tmp_path / "v.mp4").write_bytes(b"mp4")
    slept = []
    p = write(tmp_path, "2026-11-20-v.md", "channels: [tg]\nvideo: v.mp4\n")
    assert max_post.publish_one(p, "tok", "-100", sleep=slept.append) == "mid.42"
    assert slept == [3, 5]
    assert api.calls[-1][3]["attachments"][0] == {"type": "video", "payload": {"token": "vtok"}}


def test_bad_button_ignored(tmp_path, monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(max_post, "max_call", api)
    p = write(tmp_path, "2026-11-20-a.md", "channels: [tg]\nmax_button: {text: X, url: 'javascript:1'}\n")
    max_post.publish_one(p, "tok", "-100")
    assert "attachments" not in api.calls[-1][3]


def test_too_long_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(max_post, "max_call", FakeApi())
    p = write(tmp_path, "2026-11-20-a.md", "channels: [tg]\n", body="я" * 4001)
    with pytest.raises(ValueError):
        max_post.publish_one(p, "tok", "-100")


def test_main_skips_without_secrets(monkeypatch, capsys):
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MAX_CHANNEL_ID", raising=False)
    assert max_post.main() == 0
    assert "пропущен" in capsys.readouterr().out


def test_ca_bundle_contains_mincifry_cert():
    text = Path(max_post.ca_bundle()).read_text(encoding="utf-8")
    assert max_post.MAX_CA_PEM.strip() in text
    assert text.count("BEGIN CERTIFICATE") > 10
