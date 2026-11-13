"""Разовая досылка в Telegram поста, который уже отмечен «опубликованным»,
но в канал не дошёл (``tg_delivery: uncertain``). VK/YouTube/Meta не трогает.

    python -m src.tg_resend <slug>

Окружение: TG_BOT_TOKEN, TG_CHANNEL_ID. Защита от дубля: шлёт только если у файла
``tg_delivery: uncertain`` (или задан FORCE=1).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import frontmatter

from src import tg_post

REPO_ROOT = tg_post.REPO_ROOT
PUBLISHED_DIR = tg_post.PUBLISHED_DIR


def find_published(slug: str) -> Path:
    cands = sorted(PUBLISHED_DIR.glob(f"*-{slug}.md"))
    if not cands:
        raise SystemExit(f"published/*-{slug}.md не найден")
    return cands[-1]


def resend(slug: str, token: str, chat_id: str, force: bool = False) -> Path:
    path = find_published(slug)
    post = frontmatter.load(path)
    if post.metadata.get("tg_delivery") != "uncertain" and not force:
        raise SystemExit(
            f"{path.name}: tg_delivery={post.metadata.get('tg_delivery')!r} — "
            "доставка не помечена как неопределённая; FORCE=1 чтобы всё равно отправить"
        )
    body = post.content.strip()
    video_rel = post.metadata.get("video")
    split = len(body) > tg_post.TG_CAPTION_LIMIT
    caption = "" if split else body
    if video_rel:
        video_path = (REPO_ROOT / video_rel).resolve()
        res = tg_post.tg_post_video(token, chat_id, video_path, caption)
        if split:
            tg_post.tg_post_text(token, chat_id, body)
    else:
        res = tg_post.tg_post_text(token, chat_id, body)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram API returned not-ok: {res}")
    post.metadata["tg_delivery"] = "resent"
    post.metadata["tg_resent_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    post.metadata.pop("tg_uncertain_reason", None)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    print(f"✓ {path.name} → Telegram (msg_id={res['result']['message_id']})")
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("usage: python -m src.tg_resend <slug>")
    resend(argv[0], tg_post.env("TG_BOT_TOKEN"), tg_post.env("TG_CHANNEL_ID"),
           force=os.environ.get("FORCE") == "1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
