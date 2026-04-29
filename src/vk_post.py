"""Forton Lab — VK community wall publisher.

Reads the SAME queue/<slug>.md files as tg_post.py — but should be run
AFTER tg_post.py (or alongside via separate workflow step). Since tg_post.py
moves files queue → published as it succeeds, vk_post.py looks at recently
published files and posts them to VK if they haven't been posted there yet.

Tracking: a file is considered "VK-posted" if its frontmatter contains
`vk_post_id: <int>`. After successful posting, vk_post.py rewrites the file
in published/ to add this field.

Environment:
    VK_GROUP_TOKEN  — community access token with wall+photos+manage scope
    VK_GROUP_ID     — numeric group id (e.g. 238188721)

Notes on VK API:
    - Posting from group: wall.post with owner_id=-GROUP_ID, from_group=1.
    - Photo attachments: photos.getWallUploadServer → upload → photos.saveWallPhoto.
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
QUEUE_DIR = REPO_ROOT / "queue"
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


def upload_photo(token: str, group_id: str, image_path: Path) -> str:
    """Upload an image to the community wall photo server, return attachment id."""
    server = vk_call("photos.getWallUploadServer", token, group_id=group_id)
    upload_url = server["upload_url"]

    with image_path.open("rb") as f:
        files = {"photo": (image_path.name, f, "image/png")}
        r = requests.post(upload_url, files=files, timeout=60)
    r.raise_for_status()
    upload_response = r.json()

    saved = vk_call(
        "photos.saveWallPhoto",
        token,
        group_id=group_id,
        photo=upload_response["photo"],
        server=upload_response["server"],
        hash=upload_response["hash"],
    )
    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


def vk_wall_post(token: str, group_id: str, message: str, attachment: str | None = None) -> int:
    """Post to community wall as the community itself. Returns post_id."""
    params = {
        "owner_id": f"-{group_id}",
        "from_group": 1,
        "message": message,
    }
    if attachment:
        params["attachments"] = attachment
    result = vk_call("wall.post", token, **params)
    return result["post_id"]


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
    """Post a single file to VK. Returns vk post_id."""
    post = frontmatter.load(post_path)
    body = post.content.strip()
    image_rel = post.metadata.get("image")

    if not body and not image_rel:
        raise ValueError(f"{post_path.name}: nothing to post")

    if len(body) > VK_TEXT_LIMIT:
        raise ValueError(f"{post_path.name}: body too long for VK ({len(body)} > {VK_TEXT_LIMIT})")

    attachment = None
    if image_rel:
        image_path = (REPO_ROOT / image_rel).resolve()
        if not image_path.exists():
            raise FileNotFoundError(f"image not found: {image_path}")
        attachment = upload_photo(token, group_id, image_path)

    post_id = vk_wall_post(token, group_id, body, attachment)

    # Persist vk_post_id back into the file
    post.metadata["vk_post_id"] = post_id
    post.metadata["vk_posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with post_path.open("w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    print(f"✓ {post_path.name} → VK wall post_id={post_id}")
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
