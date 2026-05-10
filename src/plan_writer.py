"""plan_writer — GitHub API mutation, frontmatter rewrite, anti-replay sha8.

All write-operations to the marketing-v3 repo go through this module via
GitHub Contents API + workflow_dispatch (NOT git push). The bot reads
BOT_DISPATCH_PAT from env; tests mock requests.* directly.

Public API:
    fetch_file(pat, owner, repo, path, ref="main") -> (text, blob_sha)
    commit_file(pat, owner, repo, path, content, blob_sha, message,
                branch="main") -> commit_sha
    mutate_frontmatter_to_approved(plan_text, approver) -> str
    approve_plan(plan_path, repo_root, month, approver=...) -> commit_sha
    dispatch_regenerate(pat, owner, repo, workflow=..., ref=..., inputs=...) -> None
    plan_sha8(plan_path: Path) -> str  (8 lowercase hex chars)

    # Re-exports from spend_tracker_v2 (single import surface for Plan 04)
    read_regen_count(spend_file, month) -> int
    read_regen_limit(spend_file, default=3) -> int
    DEFAULT_REGEN_LIMIT

Errors:
    GitHubAPIError -- raised on any non-success HTTP response from GH API.
                      NEVER includes the PAT in its message (T-1.5-05-A).

Threat-model anchors:
    T-1.5-03 -- commit_file raises on 409 so caller can prompt user.
    T-1.5-04 -- plan_sha8 is the deterministic identity for callback_data.
    T-1.5-05 -- _gh_headers is the only place the PAT enters HTTP, never logged.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
from pathlib import Path

import frontmatter
import requests

# Re-export so Plan 04 can do `from src.plan_writer import read_regen_count`
# instead of importing both modules.
from src.spend_tracker_v2 import (  # noqa: F401
    DEFAULT_REGEN_LIMIT,
    read_regen_count,
    read_regen_limit,
)

GH_API = "https://api.github.com"
APPROVED_BY_DEFAULT = "forton-via-tg-bot"


class GitHubAPIError(Exception):
    """Raised on non-2xx response from GitHub Contents/Actions API.

    Message format: "<METHOD> <path> -> <status>: <body-snippet>".
    NEVER carries the PAT (Authorization header is the only place PAT lives).
    """


def _gh_headers(pat: str) -> dict:
    """Build standard GH REST headers. PAT goes ONLY into Authorization header.

    No logging or stringification of the returned dict should ever happen
    outside the requests library itself (which uses TLS).
    """
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {pat}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "forton-lab-monthly-approval-bot/1",
    }


# ----------------------------------------------------------
# GH Contents API -- read
# ----------------------------------------------------------

def fetch_file(pat: str, owner: str, repo: str, path: str,
               ref: str = "main") -> tuple[str, str]:
    """GET /repos/{o}/{r}/contents/{path}; returns (decoded_text, blob_sha).

    blob_sha is required for the subsequent commit_file PUT (optimistic
    concurrency). On non-200 raises GitHubAPIError.
    """
    r = requests.get(
        f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=_gh_headers(pat),
        params={"ref": ref},
        timeout=30,
    )
    if r.status_code != 200:
        raise GitHubAPIError(
            f"GET {path} -> {r.status_code}: {r.text[:200]}"
        )
    payload = r.json()
    text = base64.b64decode(payload["content"]).decode("utf-8")
    return text, payload["sha"]


# ----------------------------------------------------------
# GH Contents API -- write (with optimistic concurrency)
# ----------------------------------------------------------

def commit_file(pat: str, owner: str, repo: str, path: str,
                content: str, blob_sha: str, message: str,
                branch: str = "main") -> str:
    """PUT /repos/{o}/{r}/contents/{path}; returns commit SHA.

    blob_sha must match what fetch_file returned. If file changed since
    fetch (e.g. regenerate workflow committed in parallel), GH returns 409
    and we raise GitHubAPIError so caller can tell the user 'план изменился'
    (T-1.5-03 / T-1.5-04 mitigation).
    """
    r = requests.put(
        f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=_gh_headers(pat),
        json={
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": blob_sha,
            "branch": branch,
        },
        timeout=30,
    )
    if r.status_code == 409:
        raise GitHubAPIError(
            f"PUT {path} -> 409 Conflict (file changed since GET); retry needed"
        )
    if r.status_code not in (200, 201):
        raise GitHubAPIError(
            f"PUT {path} -> {r.status_code}: {r.text[:200]}"
        )
    return r.json()["commit"]["sha"]


# ----------------------------------------------------------
# Frontmatter mutation
# ----------------------------------------------------------

def mutate_frontmatter_to_approved(plan_text: str,
                                    approver: str = APPROVED_BY_DEFAULT) -> str:
    """Rewrite ONLY the top-level frontmatter:
        status: draft -> status: approved
        + approved_at: <ISO UTC>
        + approved_by: <approver>

    Per-day fenced ```yaml blocks (inside body content) are NOT touched.
    python-frontmatter parses the top `---...---` block as metadata and
    treats everything below as `content` (string), which we don't mutate.
    """
    post = frontmatter.loads(plan_text)
    post.metadata["status"] = "approved"
    post.metadata["approved_at"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    post.metadata["approved_by"] = approver
    return frontmatter.dumps(post)


# ----------------------------------------------------------
# High-level: approve_plan = fetch + mutate + commit
# ----------------------------------------------------------

def approve_plan(plan_path: Path, repo_root: Path, month: str,
                 approver: str = APPROVED_BY_DEFAULT) -> str:
    """Orchestrate: GET file -> mutate frontmatter -> PUT commit.

    Reads BOT_DISPATCH_PAT (and optionally REPO_OWNER/REPO_NAME) from env.
    Returns commit SHA. Raises KeyError if BOT_DISPATCH_PAT missing.
    """
    pat = os.environ["BOT_DISPATCH_PAT"]
    owner = os.environ.get("REPO_OWNER", "Carbon1777")
    repo = os.environ.get("REPO_NAME", "forton-lab-marketing")
    path = str(plan_path.relative_to(repo_root))

    text, blob_sha = fetch_file(pat, owner, repo, path)
    new_text = mutate_frontmatter_to_approved(text, approver=approver)
    commit_sha = commit_file(
        pat, owner, repo, path, new_text, blob_sha,
        f"chore(plan): approved by Forton via TG bot for {month}",
    )
    return commit_sha


# ----------------------------------------------------------
# GH Actions workflow_dispatch
# ----------------------------------------------------------

def dispatch_regenerate(pat: str, owner: str, repo: str,
                        workflow: str = "monthly_plan.yml",
                        ref: str = "main",
                        inputs: dict | None = None) -> None:
    """POST /repos/{o}/{r}/actions/workflows/{w}/dispatches.

    GH replies 204 No Content on success; we return None.
    Non-204 -> GitHubAPIError.
    """
    body = {"ref": ref, "inputs": inputs or {}}
    r = requests.post(
        f"{GH_API}/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
        headers=_gh_headers(pat),
        json=body,
        timeout=30,
    )
    if r.status_code != 204:
        raise GitHubAPIError(
            f"workflow_dispatch {workflow} -> {r.status_code}: {r.text[:200]}"
        )


# ----------------------------------------------------------
# plan_sha8 -- anti-replay foundation (T-1.5-04)
# ----------------------------------------------------------

def plan_sha8(plan_path: Path) -> str:
    """First 8 hex chars of sha256 of plan file bytes.

    Used in callback_data to detect stale callbacks after regenerate.
    Deterministic: same bytes -> same sha8 across processes / machines.
    """
    h = hashlib.sha256()
    with open(plan_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:8]
