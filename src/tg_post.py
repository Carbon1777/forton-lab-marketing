"""Forton Lab — Telegram publisher.

Reads posts from queue/<slug>.md, publishes them to the Telegram channel,
and moves successfully-published files to published/<YYYY-MM-DD>-<slug>.md.

A queue file is a Markdown document with YAML frontmatter:

    ---
    title: Optional title (used if no body, or as part of caption)
    image: assets/seal.png   # optional, path relative to repo root
    ---

    Post body in plain text or basic HTML.

Environment:
    TG_BOT_TOKEN   — bot token (admin in target channel)
    TG_CHANNEL_ID  — channel username (e.g. @fortonlab) or numeric chat_id

Exit codes:
    0  success (zero or more posts published)
    1  hard error (missing env, TG API failure on a post)
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import frontmatter
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = REPO_ROOT / "queue"
PUBLISHED_DIR = REPO_ROOT / "published"

TG_API_BASE = "https://api.telegram.org"
TG_TEXT_LIMIT = 4096       # sendMessage
TG_CAPTION_LIMIT = 1024    # sendPhoto / sendVideo
TG_VIDEO_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB via sendVideo on hosted Bot API


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: env {name} is missing\n")
        sys.exit(1)
    return val


def tg_post_text(token: str, chat_id: str, text: str) -> dict:
    url = f"{TG_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_post_photo(token: str, chat_id: str, photo_path: Path, caption: str) -> dict:
    url = f"{TG_API_BASE}/bot{token}/sendPhoto"
    with photo_path.open("rb") as f:
        files = {"photo": (photo_path.name, f, "image/png")}
        data = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        r = requests.post(url, data=data, files=files, timeout=60)
    r.raise_for_status()
    return r.json()


def tg_post_video(token: str, chat_id: str, video_path: Path, caption: str) -> dict:
    """Send a video file as a video message (not a document).

    Hosted Bot API accepts video files up to 50 MB. Above that, the call fails
    with HTTP 413 — switch to a self-hosted bot API server or compress.

    `supports_streaming=True` lets Telegram serve the file as a streamable
    video (preview frame, scrubbing) instead of a generic file attachment.
    """
    url = f"{TG_API_BASE}/bot{token}/sendVideo"
    size = video_path.stat().st_size
    if size > TG_VIDEO_SIZE_LIMIT:
        raise ValueError(
            f"{video_path.name}: {size} bytes exceeds Telegram hosted Bot API "
            f"50 MB limit for sendVideo. Compress with ffmpeg or split."
        )
    with video_path.open("rb") as f:
        files = {"video": (video_path.name, f, "video/mp4")}
        data = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }
        r = requests.post(url, data=data, files=files, timeout=300)
    r.raise_for_status()
    return r.json()


def publish_one(post_path: Path, token: str, chat_id: str) -> Path:
    """Publish a single queue file. Returns its new path under published/.

    Frontmatter fields:
      image: <path>   — send as photo with caption.
      video: <path>   — send as video with caption (mutually exclusive with image).
      (none)          — send body as plain text message.

    If both `image` and `video` are present, `video` wins (with a warning).
    """
    post = frontmatter.load(post_path)
    body = post.content.strip()
    image_rel = post.metadata.get("image")
    video_rel = post.metadata.get("video")

    if not body and not image_rel and not video_rel:
        raise ValueError(f"{post_path.name}: empty body, no image, no video — nothing to post")

    if video_rel and image_rel:
        sys.stderr.write(
            f"WARN: {post_path.name}: both `video` and `image` set in frontmatter; "
            "using video, ignoring image.\n"
        )

    if video_rel:
        video_path = (REPO_ROOT / video_rel).resolve()
        if not video_path.exists():
            raise FileNotFoundError(f"video not found: {video_path}")
        if len(body) > TG_CAPTION_LIMIT:
            sys.stderr.write(
                f"WARN: {post_path.name}: caption {len(body)} > {TG_CAPTION_LIMIT}; "
                "Telegram will reject. Consider splitting body into a follow-up text post.\n"
            )
        result = tg_post_video(token, chat_id, video_path, body)
    elif image_rel:
        image_path = (REPO_ROOT / image_rel).resolve()
        if not image_path.exists():
            raise FileNotFoundError(f"image not found: {image_path}")
        if len(body) > TG_CAPTION_LIMIT:
            sys.stderr.write(
                f"WARN: {post_path.name}: caption {len(body)} > {TG_CAPTION_LIMIT}; "
                "Telegram will reject. Consider splitting body and image into two posts.\n"
            )
        result = tg_post_photo(token, chat_id, image_path, body)
    else:
        if len(body) > TG_TEXT_LIMIT:
            raise ValueError(
                f"{post_path.name}: body {len(body)} > {TG_TEXT_LIMIT}; split into multiple posts"
            )
        result = tg_post_text(token, chat_id, body)

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned not-ok: {result}")

    today = dt.date.today().isoformat()
    new_name = f"{today}-{post_path.stem}.md"
    new_path = PUBLISHED_DIR / new_name
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    post_path.rename(new_path)
    msg_id = result["result"]["message_id"]
    kind = "video" if video_rel else ("photo" if image_rel else "text")
    print(f"✓ {post_path.name} → published/{new_name} (msg_id={msg_id}, kind={kind})")
    return new_path


def main() -> int:
    token = env("TG_BOT_TOKEN")
    chat_id = env("TG_CHANNEL_ID")

    if not QUEUE_DIR.exists():
        print("queue/ does not exist — nothing to publish.")
        return 0

    posts = sorted(
        p for p in QUEUE_DIR.glob("*.md")
        if not p.name.startswith("_")
    )

    if not posts:
        print("queue/ is empty — nothing to publish.")
        return 0

    print(f"Found {len(posts)} queued post(s):")
    for p in posts:
        print(f"  - {p.name}")

    failures = 0
    for p in posts:
        try:
            publish_one(p, token, chat_id)
        except Exception as e:
            sys.stderr.write(f"FAIL {p.name}: {e}\n")
            failures += 1

    if failures:
        sys.stderr.write(f"\n{failures} post(s) failed.\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
