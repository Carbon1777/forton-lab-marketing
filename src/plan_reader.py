"""Forton Lab — monthly plan reader.

Parses ``marketing-v3/plans/monthly_plan_YYYY-MM.md`` files (sectional
Markdown — variant b per Phase 1 RESEARCH.md §«Plan File Format Decision»),
provides date-based lookup and sha256 verification of media references.

Used by:
    - monthly_plan_generator.py (Phase 1) — validate output before save
    - daily generator (Phase 2) — load_current_plan → get_today_entry
                                  → verify_media_sha256 → build draft

CRITICAL: ``verify_media_sha256`` defends against MAJ-9 (stale media reference).
Path resolution uses ``_safe_resolve`` to block traversal (../etc/passwd).

The on-disk format is one Markdown file per month. The file has a top-level
YAML frontmatter (month metadata) followed by a body containing N sections,
each headed by ``## YYYY-MM-DD`` and containing one fenced ``yaml`` block
(per-entry metadata) plus free-form post body text::

    ---
    month: 2026-06
    generated_at: 2026-06-01T07:23:14Z
    generator: monthly_plan_generator v1
    ---

    # План публикаций — июнь 2026

    ## 2026-06-01

    ```yaml
    slug: forton-jun1
    channels: [tg, vk, yt, dzen]
    product: forton-lab
    rubric: from_studio
    media:
      - path: marketing-v3/assets/jun1.mp4
        sha256: a1b2c3...
        role: video
    status: draft
    ```

    Body text...

Public API (re-exported via ``__all__``):

    Datatypes:    Plan, PlanEntry, Media, Mismatch
    Exceptions:   PlanFormatError, PathTraversalError
    Parsing:      parse_plan, parse_plan_text
    Lookup:       get_today_entry, get_entry_by_date
    Verify:       verify_media_sha256, sha256_of_file
    Discovery:    discover_plans, load_current_plan

``parse_plan_text`` is intentionally PUBLIC (no underscore prefix) so Plan 04
(monthly_plan_generator) can validate generator output before writing it to
disk, without going through a tempfile dance.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml


__all__ = [
    "Plan",
    "PlanEntry",
    "Media",
    "Mismatch",
    "PlanFormatError",
    "PathTraversalError",
    "parse_plan",
    "parse_plan_text",
    "get_today_entry",
    "get_today_entries",
    "get_entry_by_date",
    "verify_media_sha256",
    "sha256_of_file",
    "discover_plans",
    "load_current_plan",
    "REPO_ROOT",
    "DEFAULT_PLANS_DIR",
]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent  # marketing-v3/
DEFAULT_PLANS_DIR = REPO_ROOT / "plans"

# Section header for one daily entry: `## 2026-06-01` on its own line.
ENTRY_HEADER = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.M)

# Fenced ```yaml ... ``` block immediately followed by free-form body text,
# until the next `## ` section or end-of-string.
YAML_BLOCK = re.compile(
    r"^```yaml\n(.*?)\n```\s*\n?(.*?)(?=\n## |\Z)",
    re.S | re.M,
)

# Top-level frontmatter `month` must look like 2026-06.
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlanFormatError(Exception):
    """Raised when a plan file violates the canonical sectional Markdown format."""


class PathTraversalError(Exception):
    """Raised when a media path resolves outside the repo root."""


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Media:
    """One media file referenced from a plan entry.

    Attributes:
        path:   Path relative to repo root (e.g. ``marketing-v3/assets/x.png``
                or ``assets/x.png`` — both resolved against ``repo_root``
                argument of :func:`verify_media_sha256`).
        sha256: 64-char lowercase hex digest of file bytes (SHA-256).
        role:   ``"image" | "video" | "thumb"``.
    """

    path: str
    sha256: str
    role: str = "image"


@dataclasses.dataclass(frozen=True)
class PlanEntry:
    """One day's planned post."""

    date: dt.date
    slug: str
    channels: list[str]
    product: str | None
    rubric: str | None
    media: list[Media]
    status: str
    content: str


@dataclasses.dataclass(frozen=True)
class Plan:
    """A full month's plan: top-level metadata + N daily entries."""

    month: str
    entries: list[PlanEntry]
    generated_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class Mismatch:
    """Result of a single media verification failure.

    ``reason`` is one of:

    - ``"missing"``       — file does not exist on disk.
    - ``"checksum_diff"`` — file exists but sha256 differs from plan.
    - ``"traversal"``     — path escapes repo root (refused before file open).
    """

    media: Media
    actual_sha256: str | None
    reason: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> dt.datetime:
    """Best-effort ISO-8601 datetime parser.

    Accepts ``datetime`` (returned as-is), strings with ``Z`` suffix,
    or strings with explicit ``+HH:MM`` offset. On any failure returns
    ``datetime.now(tz=UTC)``.
    """
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return dt.datetime.now(tz=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return dt.datetime.now(tz=dt.timezone.utc)


def parse_plan_text(text: str, plan_path_for_meta: Path) -> Plan:
    """Pure parser — accepts raw plan text and returns a :class:`Plan`.

    Used by:
    - :func:`parse_plan` (file-system entry point).
    - Phase 1 monthly_plan_generator (validates LLM output before saving).

    ``plan_path_for_meta`` is currently informational — kept in the signature
    so callers can pass it for richer error messages in future revisions.
    """
    _ = plan_path_for_meta  # reserved for future error context
    post = frontmatter.loads(text)
    month_meta = dict(post.metadata)

    month = month_meta.get("month")
    if not month or not _MONTH_RE.fullmatch(str(month)):
        raise PlanFormatError(
            f"top frontmatter `month` missing or invalid: {month!r}"
        )

    generated_at = _parse_dt(month_meta.get("generated_at"))
    body = post.content

    entries: list[PlanEntry] = []
    parts = ENTRY_HEADER.split(body)
    # parts = [preamble, date1, body1, date2, body2, ...]
    for i in range(1, len(parts), 2):
        date_str = parts[i]
        section = parts[i + 1]
        m = YAML_BLOCK.search(section)
        if not m:
            raise PlanFormatError(
                f"date {date_str}: missing ```yaml fenced block"
            )
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:  # malformed YAML inside the fence
            raise PlanFormatError(
                f"date {date_str}: malformed YAML — {exc}"
            ) from exc
        if not isinstance(meta, dict):
            raise PlanFormatError(
                f"date {date_str}: yaml block must be a mapping, got {type(meta).__name__}"
            )
        content = m.group(2).strip()

        media_raw = meta.get("media") or []
        media: list[Media] = []
        for md in media_raw:
            if not isinstance(md, dict):
                raise PlanFormatError(
                    f"date {date_str}: media item must be a mapping"
                )
            media.append(
                Media(
                    path=str(md["path"]),
                    sha256=str(md["sha256"]),
                    role=str(md.get("role", "image")),
                )
            )

        if "slug" not in meta:
            raise PlanFormatError(f"date {date_str}: missing `slug`")

        entries.append(
            PlanEntry(
                date=dt.date.fromisoformat(date_str),
                slug=str(meta["slug"]),
                channels=list(meta.get("channels", []) or []),
                product=meta.get("product"),
                rubric=meta.get("rubric"),
                media=media,
                status=str(meta.get("status", "draft")),
                content=content,
            )
        )

    return Plan(month=str(month), entries=entries, generated_at=generated_at)


def parse_plan(plan_path: Path) -> Plan:
    """Read a plan file from disk and parse it."""
    text = Path(plan_path).read_text(encoding="utf-8")
    return parse_plan_text(text, Path(plan_path))


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_today_entry(plan: Plan, today: dt.date) -> PlanEntry | None:
    """Return the first entry whose date matches ``today``, else ``None``."""
    return get_entry_by_date(plan, today)


def get_today_entries(plan: Plan, today: dt.date) -> list[PlanEntry]:
    """Return ALL entries for ``today`` (multi-post per day, GEN-04).

    A single date may have N entries (1-3) with different slugs. Existing
    :func:`get_today_entry` (single) is preserved for backward-compat with
    Phase 1/1.5 callers.

    Args:
        plan:  Parsed Plan object (from parse_plan / parse_plan_text).
        today: date to filter by.

    Returns:
        List of PlanEntry objects whose ``.date == today``, in source order.
        Empty list if no entries match.
    """
    return [e for e in plan.entries if e.date == today]


def get_entry_by_date(plan: Plan, target: dt.date) -> PlanEntry | None:
    """Return the first entry whose date == ``target``, else ``None``."""
    for entry in plan.entries:
        if entry.date == target:
            return entry
    return None


# ---------------------------------------------------------------------------
# sha256 + verify
# ---------------------------------------------------------------------------


def sha256_of_file(path: Path, chunk_size: int = 1 << 16) -> str:
    """Stream-hash a file and return lowercase hex digest (64 chars).

    Verified equivalent to ``shasum -a 256 <path>`` (macOS) and
    ``sha256sum <path>`` (Linux). Streams 64 KiB chunks so 50 MB videos
    do not allocate the whole file in memory (defends T-1-17 — DoS via
    huge-media OOM).
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``repo_root``, refusing path traversal.

    Raises :class:`PathTraversalError` if the resolved path is not strictly
    inside ``repo_root`` (catches both ``../../etc/passwd`` and absolute
    ``/etc/passwd`` style references).
    """
    root_resolved = Path(repo_root).resolve()
    candidate = (root_resolved / rel_path).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise PathTraversalError(f"path escapes repo root: {rel_path}")
    return candidate


def verify_media_sha256(entry: PlanEntry, repo_root: Path) -> list[Mismatch]:
    """Verify every media reference in ``entry``.

    For each media item in ``entry.media``:

    1. Try to resolve the path safely. On :class:`PathTraversalError`,
       record a ``"traversal"`` mismatch and skip the file (we never open it).
    2. If the file does not exist on disk → ``"missing"`` mismatch.
    3. Otherwise compute sha256; if it does not match the planned digest
       (case-insensitive) → ``"checksum_diff"`` mismatch with ``actual_sha256``.

    Returns a (possibly empty) list of mismatches. The caller is expected to
    hard-fail publication if the list is non-empty.
    """
    mismatches: list[Mismatch] = []
    for m in entry.media:
        try:
            safe = _safe_resolve(repo_root, m.path)
        except PathTraversalError:
            mismatches.append(Mismatch(media=m, actual_sha256=None,
                                       reason="traversal"))
            continue

        if not safe.exists():
            mismatches.append(Mismatch(media=m, actual_sha256=None,
                                       reason="missing"))
            continue

        actual = sha256_of_file(safe)
        if actual.lower() != m.sha256.lower():
            mismatches.append(Mismatch(media=m, actual_sha256=actual,
                                       reason="checksum_diff"))

    return mismatches


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_plans(plans_dir: Path) -> list[Path]:
    """Return all ``monthly_plan_*.md`` files in ``plans_dir`` sorted by name.

    Filename includes ``YYYY-MM`` so lexical sort == chronological sort.
    """
    return sorted(Path(plans_dir).glob("monthly_plan_*.md"))


def load_current_plan(plans_dir: Path,
                      today: dt.date | None = None) -> Plan | None:
    """Return the parsed plan for today's month, or ``None`` if absent.

    The expected filename is ``monthly_plan_{today:%Y-%m}.md``.
    """
    today = today or dt.date.today()
    month_str = today.strftime("%Y-%m")
    candidate = Path(plans_dir) / f"monthly_plan_{month_str}.md"
    if candidate.exists():
        return parse_plan(candidate)
    return None


# ---------------------------------------------------------------------------
# CLI utility — `python -m plan_reader recompute --slug X`
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="plan_reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recompute",
                       help="recompute sha256 for media in entry")
    r.add_argument("--slug", required=True)
    r.add_argument("--month", help="YYYY-MM, defaults to current")
    args = ap.parse_args()

    if args.cmd == "recompute":
        month = args.month or dt.date.today().strftime("%Y-%m")
        plan_path = DEFAULT_PLANS_DIR / f"monthly_plan_{month}.md"
        if not plan_path.exists():
            sys.stderr.write(f"plan not found: {plan_path}\n")
            sys.exit(1)
        plan = parse_plan(plan_path)
        entry = next((e for e in plan.entries if e.slug == args.slug), None)
        if not entry:
            sys.stderr.write(f"slug {args.slug!r} not in plan\n")
            sys.exit(1)
        # Round-trip YAML rewriting is non-trivial — for Phase 1 we just
        # print the new sha256 and let the user paste it.
        for m in entry.media:
            try:
                safe = _safe_resolve(REPO_ROOT, m.path)
            except PathTraversalError:
                print(f"{m.path}\n  TRAVERSAL — refusing to hash")
                continue
            new_sha = sha256_of_file(safe) if safe.exists() else "MISSING"
            print(f"{m.path}\n  expected: {m.sha256}\n  actual:   {new_sha}")
