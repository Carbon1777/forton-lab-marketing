"""Forton Lab — YouTube publisher (Shorts and regular videos).

Reads published/<slug>.md files that have a `video:` frontmatter field but no
`youtube_video_id` yet, and uploads each video via YouTube Data API v3 →
videos.insert. Writes back `youtube_video_id` and `youtube_posted_at` to the
file's frontmatter.

Authentication uses a refresh_token flow: client_id, client_secret and
refresh_token are stored in GitHub Secrets and exchanged for a fresh
access_token at runtime. Initial refresh_token is acquired once via the
`get_youtube_refresh_token.py` helper run locally on the user's machine.

Shorts vs regular: there is no separate API endpoint. YouTube auto-classifies
an uploaded video as a Short if its aspect ratio is 9:16 (vertical) and its
duration ≤60s. Our App-Store-preview-style videos at 886×1920 ≤30s qualify.

Frontmatter contract:
    video: assets/short/<slug>.mp4    (path relative to repo root)
    title: Optional explicit title    (else derived from filename)
    youtube_title: Optional override  (preferred for YT specifically)
    youtube_description: Optional     (else generated from body + signature)
    youtube_tags: [tag1, tag2, ...]   (optional list)
    youtube_privacy: "public"         (default; or "unlisted", "private")
    youtube_category: "22"            (default 22 = People & Blogs)

Environment:
    YT_CLIENT_ID
    YT_CLIENT_SECRET
    YT_REFRESH_TOKEN
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import frontmatter
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = REPO_ROOT / "published"

YT_TOKEN_URI = "https://oauth2.googleapis.com/token"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

YT_TITLE_LIMIT = 100
YT_DESCRIPTION_LIMIT = 5000
YT_TAGS_TOTAL_LIMIT = 500   # sum of len(tag) across tags
YT_DEFAULT_CATEGORY = "22"  # People & Blogs

SIGNATURE = (
    "\n\n"
    "—\n"
    "Forton Lab — российская студия мобильных приложений.\n"
    "Сайт: https://fortonlab.ru\n"
    "Telegram: https://t.me/fortonlab\n"
    "VK: https://vk.com/fortonlab"
)


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: env {name} is missing\n")
        sys.exit(1)
    return val


def build_credentials() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=env("YT_REFRESH_TOKEN"),
        token_uri=YT_TOKEN_URI,
        client_id=env("YT_CLIENT_ID"),
        client_secret=env("YT_CLIENT_SECRET"),
        scopes=YT_SCOPES,
    )
    creds.refresh(Request())
    return creds


def derive_title(post: frontmatter.Post, post_path: Path) -> str:
    explicit = post.metadata.get("youtube_title") or post.metadata.get("title")
    if explicit:
        return str(explicit)[:YT_TITLE_LIMIT]
    # Fallback: human-readable from filename.
    return post_path.stem.replace("-", " ").strip()[:YT_TITLE_LIMIT] or "Forton Lab"


def derive_description(post: frontmatter.Post) -> str:
    explicit = post.metadata.get("youtube_description")
    if explicit:
        body = str(explicit)
    else:
        body = post.content.strip()
    full = (body + SIGNATURE) if body else SIGNATURE.lstrip()
    return full[:YT_DESCRIPTION_LIMIT]


def derive_tags(post: frontmatter.Post) -> list[str]:
    raw = post.metadata.get("youtube_tags") or []
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    total = 0
    for t in raw:
        s = str(t).strip()
        if not s:
            continue
        if total + len(s) > YT_TAGS_TOTAL_LIMIT:
            break
        tags.append(s)
        total += len(s)
    return tags


def upload_video(
    youtube,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    category: str,
) -> str:
    """Resumable upload via videos.insert. Returns the new video_id."""
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category,
            "defaultLanguage": "ru",
            "defaultAudioLanguage": "ru",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  uploading… {pct}%")
        except HttpError as e:
            # 5xx are retryable; 4xx is a hard error.
            if 500 <= e.resp.status < 600:
                print(f"  retryable HTTP {e.resp.status}, continuing…")
                continue
            raise

    return response["id"]


def _should_publish(post: frontmatter.Post, channel: str) -> bool:
    """Phase 2 channel filter (mirror of tg_post / vk_post _should_publish).

    Backward-compat: пустой/отсутствующий ``channels:`` → True (Phase 1 legacy).
    """
    channels = post.metadata.get("channels")
    if not channels:
        return True
    return channel in channels


def find_pending_files() -> list[Path]:
    """Files in published/ with `video:` set and no `youtube_video_id` yet,
    AND channels include 'yt' (Phase 2; backward-compat если channels отсутствует).
    """
    if not PUBLISHED_DIR.exists():
        return []
    pending = []
    for p in sorted(PUBLISHED_DIR.glob("*.md")):
        post = frontmatter.load(p)
        if not post.metadata.get("video"):
            continue
        if post.metadata.get("youtube_video_id"):
            continue
        if not _should_publish(post, "yt"):
            print(f"  ↳ skip {p.name}: channels excludes 'yt'")
            continue
        pending.append(p)
    return pending


def publish_one(post_path: Path, youtube) -> str:
    post = frontmatter.load(post_path)
    video_rel = post.metadata.get("video")
    video_path = (REPO_ROOT / video_rel).resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    title = derive_title(post, post_path)
    description = derive_description(post)
    tags = derive_tags(post)
    privacy = str(post.metadata.get("youtube_privacy") or "public")
    category = str(post.metadata.get("youtube_category") or YT_DEFAULT_CATEGORY)

    print(f"→ uploading {video_path.name} as «{title}» (privacy={privacy})…")
    video_id = upload_video(
        youtube,
        video_path,
        title,
        description,
        tags,
        privacy,
        category,
    )

    post.metadata["youtube_video_id"] = video_id
    post.metadata["youtube_posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with post_path.open("w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    print(f"✓ {post_path.name} → https://youtu.be/{video_id}")
    return video_id


def main() -> int:
    pending = find_pending_files()
    if not pending:
        print("No published files with `video:` and no youtube_video_id. Nothing to do.")
        return 0

    print(f"Found {len(pending)} file(s) to upload to YouTube:")
    for p in pending:
        print(f"  - {p.name}")

    creds = build_credentials()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    failures = 0
    for p in pending:
        try:
            publish_one(p, youtube)
        except Exception as e:
            sys.stderr.write(f"FAIL {p.name}: {e}\n")
            failures += 1

    if failures:
        sys.stderr.write(f"\n{failures} file(s) failed.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
