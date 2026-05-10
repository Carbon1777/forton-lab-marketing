"""Tests for src/plan_reader.py — phase 1 PLAN-02 + PLAN-03 parser & verifier.

Covers:
- Sectional Markdown parsing (variant b): top-frontmatter + N sections `## YYYY-MM-DD`
  with fenced ```yaml block + body text.
- Date-based lookup (get_today_entry / get_entry_by_date).
- Streaming sha256 hashing (sha256_of_file).
- Media verification with traversal defense (verify_media_sha256).
- Plan discovery on disk (discover_plans / load_current_plan).

Hard-required tests (T-1-03, threat register):
- test_path_traversal_blocked
- test_verify_missing_file
- test_verify_detects_drift
- test_sha256_known_file
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest

from src.plan_reader import (
    Media,
    Mismatch,
    PathTraversalError,
    Plan,
    PlanEntry,
    PlanFormatError,
    discover_plans,
    get_entry_by_date,
    get_today_entry,
    load_current_plan,
    parse_plan,
    parse_plan_text,
    sha256_of_file,
    verify_media_sha256,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_full_month(fixtures_dir: Path) -> None:
    plan = parse_plan(fixtures_dir / "valid_plan.md")
    assert isinstance(plan, Plan)
    assert plan.month == "2026-06"
    assert len(plan.entries) == 3
    e0 = plan.entries[0]
    assert e0.date == dt.date(2026, 6, 1)
    assert e0.slug == "forton-jun1"
    assert e0.channels == ["tg", "vk", "yt", "dzen"]
    assert e0.product == "forton-lab"
    assert e0.rubric == "from_studio"
    assert e0.media == []
    assert e0.status == "draft"
    assert "Июнь — месяц углубления" in e0.content
    # entry 2
    e1 = plan.entries[1]
    assert e1.slug == "diktum-jun2-words"
    assert e1.channels == ["tg"]
    assert e1.product == "diktum"
    # entry 3
    e2 = plan.entries[2]
    assert e2.date == dt.date(2026, 6, 3)
    assert e2.slug == "centry-jun3-piter"


def test_parse_plan_text_returns_plan(sample_plan_text: str) -> None:
    plan = parse_plan_text(sample_plan_text, Path("inline.md"))
    assert plan.month == "2026-06"
    assert len(plan.entries) == 3
    # public symbol — Plan 04 will import this.
    assert plan.entries[0].slug == "forton-jun1"


def test_parse_broken_yaml(fixtures_dir: Path) -> None:
    with pytest.raises(PlanFormatError):
        parse_plan(fixtures_dir / "broken_yaml_plan.md")


def test_parse_missing_top_month() -> None:
    text = (
        "---\n"
        "generator: monthly_plan_generator v1\n"
        "_schema_version: 1\n"
        "---\n\n"
        "# Plan\n\n"
        "## 2026-06-01\n\n"
        "```yaml\n"
        "slug: x\n"
        "```\n\n"
        "body\n"
    )
    with pytest.raises(PlanFormatError, match="month"):
        parse_plan_text(text, Path("inline.md"))


def test_parse_section_without_yaml_block() -> None:
    text = (
        "---\n"
        "month: 2026-06\n"
        "---\n\n"
        "# Plan\n\n"
        "## 2026-06-01\n\n"
        "Just text without yaml fence.\n"
    )
    with pytest.raises(PlanFormatError, match="fenced"):
        parse_plan_text(text, Path("inline.md"))


def test_parse_invalid_date_in_section() -> None:
    # Note: 2026-13-99 is syntactically `^## YYYY-MM-DD$` so ENTRY_HEADER matches,
    # but dt.date.fromisoformat rejects month=13.
    text = (
        "---\n"
        "month: 2026-06\n"
        "---\n\n"
        "## 2026-13-99\n\n"
        "```yaml\n"
        "slug: x\n"
        "```\n\n"
        "body\n"
    )
    with pytest.raises(ValueError):
        parse_plan_text(text, Path("inline.md"))


def test_parse_generated_at_with_z_suffix(sample_plan_text: str) -> None:
    plan = parse_plan_text(sample_plan_text, Path("inline.md"))
    assert isinstance(plan.generated_at, dt.datetime)
    assert plan.generated_at.year == 2026
    assert plan.generated_at.month == 6


def test_parse_generated_at_missing_falls_back_to_utc_now() -> None:
    text = "---\nmonth: 2026-06\n---\n\n# Plan\n"
    plan = parse_plan_text(text, Path("inline.md"))
    assert plan.generated_at.tzinfo is not None


def test_parse_generated_at_malformed_falls_back() -> None:
    text = (
        "---\nmonth: 2026-06\ngenerated_at: not-a-date\n---\n\n# Plan\n"
    )
    plan = parse_plan_text(text, Path("inline.md"))
    assert plan.generated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_get_today_entry_found(fixtures_dir: Path) -> None:
    plan = parse_plan(fixtures_dir / "valid_plan.md")
    entry = get_today_entry(plan, dt.date(2026, 6, 2))
    assert entry is not None
    assert entry.slug == "diktum-jun2-words"


def test_get_today_no_entry(fixtures_dir: Path) -> None:
    plan = parse_plan(fixtures_dir / "valid_plan.md")
    assert get_today_entry(plan, dt.date(2026, 6, 30)) is None


def test_get_entry_by_date_first_match() -> None:
    media: list[Media] = []
    e1 = PlanEntry(
        date=dt.date(2026, 6, 1), slug="a", channels=[],
        product=None, rubric=None, media=media, status="draft", content="",
    )
    e2 = PlanEntry(
        date=dt.date(2026, 6, 1), slug="b", channels=[],
        product=None, rubric=None, media=media, status="draft", content="",
    )
    plan = Plan(month="2026-06", entries=[e1, e2],
                generated_at=dt.datetime.now(tz=dt.timezone.utc))
    found = get_entry_by_date(plan, dt.date(2026, 6, 1))
    assert found is e1


# ---------------------------------------------------------------------------
# sha256
# ---------------------------------------------------------------------------


def test_sha256_known_file(fixtures_dir: Path) -> None:
    path = fixtures_dir / "sample_image.png"
    expected = (
        subprocess.check_output(["shasum", "-a", "256", str(path)])
        .decode()
        .split()[0]
    )
    assert sha256_of_file(path) == expected


def test_sha256_streams_large_file(tmp_path: Path) -> None:
    # 200 KB > default 64KB chunk_size, exercises the streaming loop.
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (200 * 1024))
    expected = (
        subprocess.check_output(["shasum", "-a", "256", str(path)])
        .decode()
        .split()[0]
    )
    assert sha256_of_file(path) == expected


# ---------------------------------------------------------------------------
# verify_media_sha256
# ---------------------------------------------------------------------------


def test_verify_clean(tmp_repo: Path) -> None:
    f = tmp_repo / "media" / "ok.png"
    f.write_bytes(b"hello")
    sha = sha256_of_file(f)
    entry = PlanEntry(
        date=dt.date(2026, 6, 1), slug="t", channels=[],
        product=None, rubric=None,
        media=[Media(path="media/ok.png", sha256=sha, role="image")],
        status="draft", content="",
    )
    assert verify_media_sha256(entry, tmp_repo) == []


def test_verify_missing_file(tmp_repo: Path) -> None:
    entry = PlanEntry(
        date=dt.date(2026, 6, 1), slug="t", channels=[],
        product=None, rubric=None,
        media=[Media(path="media/does_not_exist.png",
                     sha256="0" * 64, role="image")],
        status="draft", content="",
    )
    result = verify_media_sha256(entry, tmp_repo)
    assert len(result) == 1
    assert result[0].reason == "missing"
    assert result[0].actual_sha256 is None


def test_verify_detects_drift(tmp_repo: Path) -> None:
    f = tmp_repo / "media" / "drift.png"
    f.write_bytes(b"original")
    original_sha = sha256_of_file(f)
    f.write_bytes(b"changed")  # подменили
    entry = PlanEntry(
        date=dt.date(2026, 6, 1), slug="t", channels=[],
        product=None, rubric=None,
        media=[Media(path="media/drift.png",
                     sha256=original_sha, role="image")],
        status="draft", content="",
    )
    result = verify_media_sha256(entry, tmp_repo)
    assert len(result) == 1
    assert result[0].reason == "checksum_diff"
    assert result[0].actual_sha256 == sha256_of_file(f)


def test_verify_case_insensitive_sha(tmp_repo: Path) -> None:
    f = tmp_repo / "media" / "case.png"
    f.write_bytes(b"data")
    sha = sha256_of_file(f).upper()  # provoke case mismatch
    entry = PlanEntry(
        date=dt.date(2026, 6, 1), slug="t", channels=[],
        product=None, rubric=None,
        media=[Media(path="media/case.png", sha256=sha, role="image")],
        status="draft", content="",
    )
    assert verify_media_sha256(entry, tmp_repo) == []


# ---------------------------------------------------------------------------
# Path-traversal defense (T-1-03 / Pitfall 5)
# ---------------------------------------------------------------------------


def test_path_traversal_blocked(tmp_repo: Path) -> None:
    entry = PlanEntry(
        date=dt.date(2026, 6, 1), slug="t", channels=[],
        product=None, rubric=None,
        media=[Media(path="../../../etc/passwd",
                     sha256="0" * 64, role="image")],
        status="draft", content="",
    )
    result = verify_media_sha256(entry, tmp_repo)
    assert len(result) == 1
    assert result[0].reason == "traversal"
    assert result[0].actual_sha256 is None


def test_path_traversal_with_absolute(tmp_repo: Path) -> None:
    entry = PlanEntry(
        date=dt.date(2026, 6, 1), slug="t", channels=[],
        product=None, rubric=None,
        media=[Media(path="/etc/passwd", sha256="0" * 64, role="image")],
        status="draft", content="",
    )
    result = verify_media_sha256(entry, tmp_repo)
    assert len(result) == 1
    assert result[0].reason == "traversal"


# ---------------------------------------------------------------------------
# discover_plans / load_current_plan
# ---------------------------------------------------------------------------


def test_load_current_plan_for_month(tmp_repo: Path,
                                     fixtures_dir: Path) -> None:
    plans_dir = tmp_repo / "plans"
    shutil.copy(fixtures_dir / "valid_plan.md",
                plans_dir / "monthly_plan_2026-06.md")
    plan = load_current_plan(plans_dir, today=dt.date(2026, 6, 15))
    assert plan is not None
    assert plan.month == "2026-06"
    # No file for 2026-07 → None.
    assert load_current_plan(plans_dir, today=dt.date(2026, 7, 15)) is None


def test_load_current_plan_default_today(tmp_repo: Path,
                                         fixtures_dir: Path) -> None:
    # When today=None, function defaults to dt.date.today() — file likely missing
    # for current month, expect None without crash.
    plans_dir = tmp_repo / "plans"
    assert load_current_plan(plans_dir) is None


def test_discover_plans_sorted(tmp_repo: Path) -> None:
    plans_dir = tmp_repo / "plans"
    (plans_dir / "monthly_plan_2026-08.md").write_text("---\nmonth: 2026-08\n---\n")
    (plans_dir / "monthly_plan_2026-06.md").write_text("---\nmonth: 2026-06\n---\n")
    (plans_dir / "monthly_plan_2026-07.md").write_text("---\nmonth: 2026-07\n---\n")
    (plans_dir / "stranger.md").write_text("ignore me")
    found = discover_plans(plans_dir)
    names = [p.name for p in found]
    assert names == [
        "monthly_plan_2026-06.md",
        "monthly_plan_2026-07.md",
        "monthly_plan_2026-08.md",
    ]
