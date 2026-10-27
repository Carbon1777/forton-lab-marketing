"""Forton Lab — Meta publisher: Instagram Reels + Facebook Page Reels.

Читает published/<slug>.md, у которых есть ``video:`` и в ``channels:``
явно указан ``ig`` и/или ``fb`` (без ``channels`` Meta НЕ включается — это
не «legacy publish everywhere»). Публикует через Graph API и пишет в
frontmatter ``ig_media_id`` / ``fb_video_id`` / ``meta_posted_at``.

Официальный путь (Graph API, без UI-ботов → нет риска бана за автоматику):

  Instagram Reels (resumable upload, Facebook Login for Business):
    1. POST /{ig-user-id}/media  media_type=REELS upload_type=resumable
    2. POST rupload.facebook.com/ig-api-upload/{ver}/{container-id}  (байты файла)
    3. GET  /{container-id}?fields=status_code  → FINISHED
    4. POST /{ig-user-id}/media_publish  creation_id=...

  Facebook Page Reels:
    1. POST /{page-id}/video_reels  upload_phase=start
    2. POST {upload_url}            (байты файла)
    3. POST /{page-id}/video_reels  upload_phase=finish video_state=PUBLISHED

Переменные окружения (GitHub Secrets):
    META_PAGE_TOKEN     — Page access token (бессрочный, из долгоживущего user-токена)
    META_PAGE_ID        — id страницы Facebook «Forton Lab»
    META_IG_USER_ID     — id Instagram Business аккаунта (forton_lab)
    META_GRAPH_VERSION  — необязательно, по умолчанию v25.0

Ограничения Reels через API: MP4/MOV, H.264, до 90 c, вертикаль 9:16.
Инструкция по получению токена — docs/VIDEO_POSTS.md.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

import frontmatter
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = REPO_ROOT / "published"

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com"
DEFAULT_VERSION = "v25.0"

IG_CAPTION_LIMIT = 2200
FB_DESCRIPTION_LIMIT = 5000
POLL_INTERVAL_S = 10
POLL_MAX_S = 600
HTTP_TIMEOUT = 120


class MetaAPIError(Exception):
    """Любая ошибка Graph API. Токен в сообщение не попадает."""


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        raise KeyError(f"environment variable {name} is not set")
    return v


def version() -> str:
    return os.environ.get("META_GRAPH_VERSION") or DEFAULT_VERSION


def strip_html(text: str) -> str:
    """TG-тело содержит <a href>, <b> и т.п.: для Instagram/Facebook — чистый текст."""
    text = re.sub(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 \1", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").strip()


def derive_caption(post: frontmatter.Post, limit: int) -> str:
    explicit = post.metadata.get("meta_caption")
    text = strip_html(str(explicit) if explicit else post.content)
    return text[:limit]


def _check(resp: requests.Response, what: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise MetaAPIError(
            f"{what}: HTTP {resp.status_code} code={err.get('code')} "
            f"sub={err.get('error_subcode')} msg={err.get('message', resp.text[:200])}"
        )
    return data


def _upload_bytes(url: str, token: str, video_path: Path) -> None:
    size = video_path.stat().st_size
    with video_path.open("rb") as f:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
            },
            data=f,
            timeout=HTTP_TIMEOUT * 5,
        )
    _check(resp, "rupload")


# ---------------------------------------------------------------------------
# Instagram Reels
# ---------------------------------------------------------------------------

def publish_instagram_reel(video_path: Path, caption: str, *, ig_user_id: str,
                           token: str, sleep=time.sleep) -> str:
    ver = version()
    r = requests.post(
        f"{GRAPH}/{ver}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=HTTP_TIMEOUT,
    )
    container_id = _check(r, "ig create container")["id"]

    _upload_bytes(f"{RUPLOAD}/ig-api-upload/{ver}/{container_id}", token, video_path)

    waited = 0
    while True:
        s = requests.get(
            f"{GRAPH}/{ver}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=HTTP_TIMEOUT,
        )
        status = _check(s, "ig container status").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise MetaAPIError(f"ig container {container_id}: status_code={status}")
        if waited >= POLL_MAX_S:
            raise MetaAPIError(f"ig container {container_id}: timeout, last status={status}")
        sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S

    p = requests.post(
        f"{GRAPH}/{ver}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=HTTP_TIMEOUT,
    )
    return str(_check(p, "ig media_publish")["id"])


# ---------------------------------------------------------------------------
# Facebook Page Reels
# ---------------------------------------------------------------------------

def publish_facebook_reel(video_path: Path, description: str, *, page_id: str,
                          token: str) -> str:
    ver = version()
    s = requests.post(
        f"{GRAPH}/{ver}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": token},
        timeout=HTTP_TIMEOUT,
    )
    start = _check(s, "fb reel start")
    video_id = str(start["video_id"])
    upload_url = start.get("upload_url") or f"{RUPLOAD}/video-upload/{ver}/{video_id}"

    _upload_bytes(upload_url, token, video_path)

    f = requests.post(
        f"{GRAPH}/{ver}/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": description,
            "access_token": token,
        },
        timeout=HTTP_TIMEOUT,
    )
    _check(f, "fb reel finish")
    return video_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _wants(post: frontmatter.Post, channel: str) -> bool:
    """Meta включается только явным указанием канала в ``channels:``."""
    return channel in (post.metadata.get("channels") or [])


def find_pending_files() -> list[Path]:
    if not PUBLISHED_DIR.exists():
        return []
    pending = []
    for p in sorted(PUBLISHED_DIR.glob("*.md")):
        post = frontmatter.load(p)
        if not post.metadata.get("video"):
            continue
        need_ig = _wants(post, "ig") and not post.metadata.get("ig_media_id")
        need_fb = _wants(post, "fb") and not post.metadata.get("fb_video_id")
        if need_ig or need_fb:
            pending.append(p)
    return pending


def publish_one(post_path: Path, *, token: str, page_id: str | None,
                ig_user_id: str | None) -> list[str]:
    """Публикует недостающие каналы. Возвращает список ошибок (пусто = всё ок)."""
    post = frontmatter.load(post_path)
    video_path = (REPO_ROOT / post.metadata["video"]).resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    errors: list[str] = []
    changed = False

    if _wants(post, "ig") and not post.metadata.get("ig_media_id"):
        try:
            if not ig_user_id:
                raise KeyError("META_IG_USER_ID is not set")
            mid = publish_instagram_reel(
                video_path, derive_caption(post, IG_CAPTION_LIMIT),
                ig_user_id=ig_user_id, token=token,
            )
            post.metadata["ig_media_id"] = mid
            changed = True
            print(f"✓ {post_path.name} → Instagram Reel {mid}")
        except Exception as exc:  # один канал не должен ронять второй
            errors.append(f"ig: {exc}")

    if _wants(post, "fb") and not post.metadata.get("fb_video_id"):
        try:
            if not page_id:
                raise KeyError("META_PAGE_ID is not set")
            vid = publish_facebook_reel(
                video_path, derive_caption(post, FB_DESCRIPTION_LIMIT),
                page_id=page_id, token=token,
            )
            post.metadata["fb_video_id"] = vid
            changed = True
            print(f"✓ {post_path.name} → Facebook Reel {vid}")
        except Exception as exc:
            errors.append(f"fb: {exc}")

    if changed:
        post.metadata["meta_posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        post_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return errors


def main() -> int:
    pending = find_pending_files()
    if not pending:
        print("Нет published/*.md с video и каналами ig/fb без id. Nothing to do.")
        return 0

    token = os.environ.get("META_PAGE_TOKEN")
    if not token:
        print("META_PAGE_TOKEN не задан — Meta пропускаем (Instagram/Facebook не настроены).")
        return 0
    page_id = os.environ.get("META_PAGE_ID")
    ig_user_id = os.environ.get("META_IG_USER_ID")

    failures = 0
    for p in pending:
        try:
            errs = publish_one(p, token=token, page_id=page_id, ig_user_id=ig_user_id)
        except Exception as exc:
            errs = [str(exc)]
        for e in errs:
            sys.stderr.write(f"FAIL {p.name}: {e}\n")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
