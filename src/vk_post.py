"""Forton Lab — VK community wall publisher (text-only).

Reads published/<slug>.md files (those that already went out via TG by tg_post.py)
and posts their text bodies to the VK community wall as the community.

Image handling — by design manual.
    VK community tokens cannot use photos.getWallUploadServer (scope `photos`
    is granted by VK only via devsupport@corp.vk.com on a case-by-case basis).
    Old user-token flow (Implicit Flow / VK ID OAuth 2.1) requires the same
    privileged scope, also via support. We accepted this and chose a manual
    workflow:

      1. While preparing a post, Cowork places the image into ~/Documents/vk_attach/
         under the same basename as the post (e.g. 2026-04-29-welcome.png).
      2. vk_post.py publishes the text-only post here.
      3. The user opens the published VK post → Edit → attaches the image
         from ~/Documents/vk_attach/ → saves → deletes the file from the folder.

    Therefore vk_post.py never touches photos.* methods.

Tracking: a file is considered "VK-posted" if its frontmatter contains
`vk_post_id: <int>`. After a successful wall.post we rewrite the file in
published/ to add this field and `vk_posted_at`.

Environment:
    VK_GROUP_TOKEN  — community access token (wall scope only is needed for text)
    VK_GROUP_ID     — numeric group id (e.g. 238188721)

API:
    - wall.post with owner_id=-GROUP_ID, from_group=1.
    - All requests use v=5.199.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import frontmatter
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = REPO_ROOT / "published"

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"
VK_TEXT_LIMIT = 16000  # wall posts are generous


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: env {name} is missing\n")
        sys.exit(1)
    return val


def vk_call(method: str, token: str, **params) -> dict:
    params["access_token"] = token
    params["v"] = VK_VERSION
    r = requests.post(f"{VK_API}/{method}", data=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"VK API error in {method}: {data['error']}")
    return data["response"]


def vk_wall_post(token: str, group_id: str, message: str) -> int:
    """Post text-only to community wall as the community. Returns post_id."""
    result = vk_call(
        "wall.post",
        token,
        owner_id=f"-{group_id}",
        from_group=1,
        message=message,
    )
    return result["post_id"]


def _should_publish(post: frontmatter.Post, channel: str) -> bool:
    """Phase 2 channel filter (mirror of tg_post._should_publish).

    Backward-compat: пустой/отсутствующий ``channels:`` → True (Phase 1 legacy).
    """
    channels = post.metadata.get("channels")
    if not channels:
        return True
    return channel in channels


def find_pending_published_files() -> list[Path]:
    """Files in published/ that don't have vk_post_id yet."""
    if not PUBLISHED_DIR.exists():
        return []
    pending = []
    for p in sorted(PUBLISHED_DIR.glob("*.md")):
        post = frontmatter.load(p)
        if not post.metadata.get("vk_post_id"):
            pending.append(p)
    return pending


def publish_one(post_path: Path, token: str, group_id: str) -> int:
    """Post a single file to VK. Returns vk post_id, or -1 if skipped by channel filter."""
    post = frontmatter.load(post_path)

    # Phase 2 channel filter — skip silently if 'vk' not in entry channels
    if not _should_publish(post, "vk"):
        print(
            f"  ↳ skip {post_path.name}: channels="
            f"{post.metadata.get('channels')!r} excludes 'vk'"
        )
        return -1

    body = post.content.strip()
    image_rel = post.metadata.get("image")
    video_rel = post.metadata.get("video")

    if not body:
        raise ValueError(f"{post_path.name}: empty body, nothing to post")

    if len(body) > VK_TEXT_LIMIT:
        raise ValueError(f"{post_path.name}: body too long for VK ({len(body)} > {VK_TEXT_LIMIT})")

    post_id = vk_wall_post(token, group_id, body)

    # Persist vk_post_id back into the file
    post.metadata["vk_post_id"] = post_id
    post.metadata["vk_posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with post_path.open("w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    print(f"✓ {post_path.name} → VK wall post_id={post_id}")
    if video_rel:
        # Manual video attach reminder. Cowork places an mp4 copy into
        # ~/Documents/vk_attach/<stem>.mp4 during post preparation.
        print(
            f"  ↳ video expected at ~/Documents/vk_attach/{post_path.stem}.mp4 "
            f"— attach manually in VK via Edit, then delete the file."
        )
    elif image_rel:
        print(
            f"  ↳ image expected at ~/Documents/vk_attach/{post_path.stem}.png "
            f"— attach manually in VK via Edit, then delete the file."
        )
    return post_id


def main() -> int:
    token = env("VK_GROUP_TOKEN")
    group_id = env("VK_GROUP_ID")

    pending = find_pending_published_files()
    if not pending:
        print("No published-but-not-VK-posted files. Nothing to do.")
        return 0

    print(f"Found {len(pending)} file(s) to post to VK:")
    for p in pending:
        print(f"  - {p.name}")

    failures = 0
    for p in pending:
        try:
            publish_one(p, token, group_id)
        except Exception as e:
            sys.stderr.write(f"FAIL {p.name}: {e}\n")
            failures += 1

    if failures:
        sys.stderr.write(f"\n{failures} file(s) failed.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
