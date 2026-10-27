"""Пакет для ручных каналов (VK-видео, Rutube): ролик + готовый текст в личку.

Для видео-постов, у которых в ``channels:`` есть ``vk`` и/или ``rutube``,
после публикации шлёт владельцу (планировщик-бот → TG_OWNER_CHAT_ID) сам
видеофайл с подписью и пометкой, куда его выложить руками:

- VK: автоматизация закрыта (error 27) — текст уходит на стену через
  ``vk_post.py``, видео крепится вручную.
- Rutube: публичного API загрузки нет — выкладывается руками в Студии.

Помечает ``manual_pkg_sent`` в frontmatter, чтобы не слать дважды.
Окружение: TG_PLANNER_BOT_TOKEN, TG_OWNER_CHAT_ID.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
from pathlib import Path

import frontmatter
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = REPO_ROOT / "published"
MANUAL_CHANNELS = ("vk", "rutube")
TG_VIDEO_LIMIT = 50 * 1024 * 1024
TG_CAPTION_LIMIT = 1024
TG_TEXT_LIMIT = 4096


def _plain(text: str) -> str:
    text = re.sub(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 \1", text, flags=re.S)
    return re.sub(r"<[^>]+>", "", text).strip()


def targets(post: frontmatter.Post) -> list[str]:
    return [c for c in MANUAL_CHANNELS if c in (post.metadata.get("channels") or [])]


def find_pending_files() -> list[Path]:
    if not PUBLISHED_DIR.exists():
        return []
    out = []
    for p in sorted(PUBLISHED_DIR.glob("*.md")):
        post = frontmatter.load(p)
        if post.metadata.get("video") and targets(post) and not post.metadata.get("manual_pkg_sent"):
            out.append(p)
    return out


def build_header(post: frontmatter.Post, slug: str) -> str:
    where = " и ".join({"vk": "VK (прикрепить видео к посту)", "rutube": "Rutube (загрузить в Студии)"}[c]
                       for c in targets(post))
    return f"📦 Ролик для ручной выкладки: {where}\nСлаг: {slug}"


def _api(token: str, method: str, **kw) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=300, **kw)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"{method} failed: HTTP {r.status_code} {str(data)[:200]}")
    return data


def send_package(post_path: Path, token: str, chat_id: str) -> None:
    post = frontmatter.load(post_path)
    slug = post_path.stem
    video_path = (REPO_ROOT / post.metadata["video"]).resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")
    if video_path.stat().st_size > TG_VIDEO_LIMIT:
        raise ValueError(f"{video_path.name} > 50 MB, в личку не отправить")

    body = _plain(post.content)
    # Видео — с короткой шапкой; полный текст отдельным сообщением, чтобы удобно копировать.
    with video_path.open("rb") as f:
        _api(token, "sendVideo",
             data={"chat_id": chat_id, "caption": build_header(post, slug)[:TG_CAPTION_LIMIT],
                   "supports_streaming": "true"},
             files={"video": (video_path.name, f, "video/mp4")})
    if body:
        _api(token, "sendMessage",
             data={"chat_id": chat_id, "text": html.unescape(body)[:TG_TEXT_LIMIT],
                   "disable_web_page_preview": "true"})

    post.metadata["manual_pkg_sent"] = dt.datetime.now(dt.timezone.utc).isoformat()
    post_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    print(f"✓ {post_path.name} → пакет для {', '.join(targets(post))} отправлен в личку")


def main() -> int:
    pending = find_pending_files()
    if not pending:
        print("Нет видео-постов для ручных каналов. Nothing to do.")
        return 0
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not token or not chat_id:
        print("TG_PLANNER_BOT_TOKEN/TG_OWNER_CHAT_ID не заданы — пакет не отправлен.")
        return 0
    failures = 0
    for p in pending:
        try:
            send_package(p, token, chat_id)
        except Exception as exc:
            sys.stderr.write(f"FAIL {p.name}: {exc}\n")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
