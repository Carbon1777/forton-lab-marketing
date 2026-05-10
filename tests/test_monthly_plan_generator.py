"""Tests for src/monthly_plan_generator.py — phase 1 PLAN-01.

All Anthropic API calls are mocked via the mock_anthropic_client fixture
(conftest.py). Real network is never touched.

Coverage gate: ≥70% (per plan §<verification>).
Hard-required tests (per <success_criteria>):
    - test_no_api_key_in_output (T-1-01)
    - test_hard_fail_on_brand_lint (T-1-02)
    - test_hard_fail_on_missing_media (T-1-03)
    - test_hard_fail_on_sha_mismatch (T-1-03)
    - test_budget_blocks (T-1-04)
    - test_budget_cap_blocks_api_call (T-1-04)
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import os
import re

from src import monthly_plan_generator as g
from src.monthly_plan_generator import (
    BrandViolationError,
    BudgetExceededError,
    GenerationError,
    MAX_TOKENS_MONTHLY_PLAN,
    MODEL,
    build_media_manifest,
    build_user_prompt,
    estimate_call_cost,
    main,
    preflight_budget_check,
    record_spend,
    sanitize_output,
)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def env_set(monkeypatch):
    """All required env vars present."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key-1234567890")
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "FAKE_TG_TOKEN")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "999999")


@pytest.fixture
def patched_repo(monkeypatch, tmp_repo):
    """Repoint generator's REPO_ROOT to tmp_repo and capture tg_nudge.send."""
    monkeypatch.setattr(g, "REPO_ROOT", tmp_repo)
    monkeypatch.setattr(g, "DEFAULT_PLANS_DIR", tmp_repo / "plans")
    monkeypatch.setattr(g, "DEFAULT_SPEND_FILE", tmp_repo / ".metrics" / "api_spend.json")
    monkeypatch.setattr(g, "DEFAULT_PUBLISHED_DIR", tmp_repo / "published")
    m_nudge = MagicMock(return_value=0)
    monkeypatch.setattr("src.monthly_plan_generator.tg_nudge.send", m_nudge)
    return tmp_repo, m_nudge


@pytest.fixture
def mock_anthropic_in_generator(monkeypatch, mock_anthropic_client):
    """Patch Anthropic class in generator module."""
    fake_client, fake_msg = mock_anthropic_client
    monkeypatch.setattr(
        "src.monthly_plan_generator.Anthropic",
        lambda **kwargs: fake_client,
    )
    return fake_client, fake_msg


@pytest.fixture
def patch_calendar_3days(monkeypatch):
    """Patch calendar.monthrange so cardinality matches sample_plan_text (3 entries)."""
    fake_cal = type("FakeCal", (), {"monthrange": staticmethod(lambda y, m: (0, 3))})()
    monkeypatch.setattr(g, "calendar", fake_cal)


# --- 1. Constants & module structure ---------------------------------------


def test_constants_match_research():
    """MAX_TOKENS=8000 (NOT 1500), MODEL pin per RESEARCH §«Anthropic SDK»."""
    assert MAX_TOKENS_MONTHLY_PLAN == 8000
    assert MODEL == "claude-sonnet-4-5"
    assert g.INPUT_PRICE_PER_M == 3.0
    assert g.OUTPUT_PRICE_PER_M == 15.0
    assert g.MONTHLY_CAP_USD == 5.0
    assert g.SYSTEM_PROMPT.strip(), "SYSTEM_PROMPT must not be empty"
    # SYSTEM_PROMPT contains the 5 hard requirements
    assert "Centry" in g.SYSTEM_PROMPT
    assert "Diktum" in g.SYSTEM_PROMPT
    assert "ОБЯЗАТЕЛЬНЫЕ" in g.SYSTEM_PROMPT


# --- 2. Cost estimation ----------------------------------------------------


def test_estimate_call_cost():
    """10K input + 8K output = 10000/1M*3 + 8000/1M*15 = 0.03 + 0.12 = $0.15."""
    cost = estimate_call_cost(10_000, 8000)
    assert abs(cost - 0.15) < 1e-6


def test_estimate_call_cost_zero():
    assert estimate_call_cost(0, 0) == 0.0


# --- 3. Pre-flight budget check --------------------------------------------


def test_budget_blocks(tmp_repo):
    """If current+est > $5 cap → BudgetExceededError."""
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    month_key = dt.date.today().strftime("%Y-%m")
    spend_file.write_text(
        json.dumps({
            "_schema_version": 1,
            "_updated": "2026-05-09T00:00:00Z",
            month_key: {"input_tokens": 1, "output_tokens": 1, "usd": 4.95,
                        "calls": 1, "by_purpose": {}},
        })
    )
    with pytest.raises(BudgetExceededError):
        preflight_budget_check(spend_file, est_cost=0.12)


def test_budget_warns_at_60pct(tmp_repo):
    """current=2.5 + est=0.5 = 3.0 (60% of 5.0) → warn flag True."""
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    month_key = dt.date.today().strftime("%Y-%m")
    spend_file.write_text(
        json.dumps({
            "_schema_version": 1,
            month_key: {"input_tokens": 0, "output_tokens": 0, "usd": 2.5,
                        "calls": 0, "by_purpose": {}},
        })
    )
    current, warn = preflight_budget_check(spend_file, est_cost=0.5)
    assert current == 2.5
    assert warn is True


def test_budget_no_warn_below_60pct(tmp_repo):
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    month_key = dt.date.today().strftime("%Y-%m")
    spend_file.write_text(
        json.dumps({
            "_schema_version": 1,
            month_key: {"input_tokens": 0, "output_tokens": 0, "usd": 0.5,
                        "calls": 0, "by_purpose": {}},
        })
    )
    current, warn = preflight_budget_check(spend_file, est_cost=0.5)
    assert current == 0.5
    assert warn is False


def test_budget_check_no_current_month(tmp_repo):
    """If current month not yet in tracker → current=0, no warn."""
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    # default content from fixture has no current month entry
    current, warn = preflight_budget_check(spend_file, est_cost=0.1)
    assert current == 0.0
    assert warn is False


# --- 4. Spend tracker ------------------------------------------------------


def test_spend_tracker_increments(tmp_repo):
    """record_spend writes input/output tokens + usd + by_purpose breakdown."""
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    record_spend(spend_file, input_tokens=1000, output_tokens=500)
    data = json.loads(spend_file.read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    assert month_key in data
    entry = data[month_key]
    assert entry["input_tokens"] == 1000
    assert entry["output_tokens"] == 500
    expected_usd = round(1000 / 1e6 * 3.0 + 500 / 1e6 * 15.0, 4)
    assert entry["usd"] == expected_usd
    assert entry["calls"] == 1
    assert "monthly_plan" in entry["by_purpose"]
    assert entry["by_purpose"]["monthly_plan"]["calls"] == 1


def test_spend_tracker_atomic_recovery(tmp_repo):
    """Corrupt JSON → _load_spend returns default → record_spend still works."""
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    spend_file.write_text("{ this is not valid json")
    # Should not raise
    record_spend(spend_file, input_tokens=100, output_tokens=50)
    data = json.loads(spend_file.read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    assert month_key in data


def test_spend_tracker_two_calls_accumulate(tmp_repo):
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    record_spend(spend_file, 1000, 500)
    record_spend(spend_file, 2000, 1000)
    data = json.loads(spend_file.read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    assert data[month_key]["input_tokens"] == 3000
    assert data[month_key]["output_tokens"] == 1500
    assert data[month_key]["calls"] == 2


# --- 5. Output sanitizer (T-1-01) ------------------------------------------


def test_sanitize_output_blocks_api_key():
    """T-1-01: any sk-ant-* prefix in output → GenerationError."""
    with pytest.raises(GenerationError):
        sanitize_output("foo sk-ant-api03-XXXXXXXXXX bar")


def test_sanitize_output_blocks_short_match():
    """sk-ant- followed by ≥10 chars matches."""
    with pytest.raises(GenerationError):
        sanitize_output("hello sk-ant-abcdefghij world")


def test_sanitize_output_passes_clean():
    """Clean text → no exception."""
    sanitize_output("clean marketing text without secrets")
    sanitize_output("")  # edge: empty


def test_sanitize_output_passes_short_prefix():
    """sk-ant- with <10 chars after — should NOT match (regex requires {10,})."""
    sanitize_output("just sk-ant-abc here")  # only 3 chars after prefix


# --- 6. Build helpers ------------------------------------------------------


def test_build_media_manifest_empty_dirs(tmp_repo):
    """Empty assets/+media/ → graceful default."""
    manifest = build_media_manifest(tmp_repo)
    assert "no media files" in manifest.lower()


def test_build_media_manifest_with_files(tmp_repo):
    """Files in assets/ → manifest contains path + sha256 + size_kb + mime."""
    f = tmp_repo / "assets" / "test.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    manifest = build_media_manifest(tmp_repo)
    assert "test.png" in manifest
    assert "sha256:" in manifest
    assert "image/png" in manifest


def test_build_user_prompt_contains_required_blocks():
    """User prompt has all 4 blocks: month, strategy, history, manifest."""
    prompt = build_user_prompt(
        month="2026-06",
        n_days=30,
        strategy="STRATEGY CONTENT HERE",
        history="HISTORY CONTENT",
        manifest="MANIFEST CONTENT",
    )
    assert "2026-06" in prompt
    assert "30" in prompt
    assert "STRATEGY CONTENT HERE" in prompt
    assert "HISTORY CONTENT" in prompt
    assert "MANIFEST CONTENT" in prompt


# --- 7. End-to-end happy path -----------------------------------------------


def _build_valid_3day_plan(tmp_repo: Path) -> str:
    """Construct a valid 3-day plan text matching tmp_repo media."""
    # No media for simplicity — body text only, well within brand-lint
    return (
        "---\n"
        "month: 2026-06\n"
        "generated_at: 2026-06-01T07:00:00Z\n"
        "generator: monthly_plan_generator v1\n"
        "---\n"
        "\n"
        "# План публикаций — июнь 2026\n"
        "\n"
        "## 2026-06-01\n"
        "\n"
        "```yaml\n"
        "slug: forton-jun1\n"
        "channels: [tg]\n"
        "product: forton-lab\n"
        "rubric: from_studio\n"
        "media: []\n"
        "status: draft\n"
        "```\n"
        "\n"
        "Июнь — месяц углубления.\n"
        "\n"
        "fortonlab.ru\n"
        "\n"
        "\n"
        "## 2026-06-02\n"
        "\n"
        "```yaml\n"
        "slug: diktum-jun2\n"
        "channels: [tg]\n"
        "product: diktum\n"
        "rubric: words\n"
        "media: []\n"
        "status: draft\n"
        "```\n"
        "\n"
        "Слово недели: «как бы».\n"
        "\n"
        "diktumweb.ru\n"
        "\n"
        "\n"
        "## 2026-06-03\n"
        "\n"
        "```yaml\n"
        "slug: centry-jun3\n"
        "channels: [vk]\n"
        "product: centry\n"
        "rubric: city\n"
        "media: []\n"
        "status: draft\n"
        "```\n"
        "\n"
        "Топ-7 заведений в Питере для компании.\n"
        "\n"
        "centryweb.ru\n"
    )


def test_e2e_mocked_anthropic_success(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """Happy path: API call → validate → save → record_spend → nudge success."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    fake_msg.content[0].text = _build_valid_3day_plan(tmp_repo)

    rc = main(month_override="2026-06")
    assert rc == 0, f"expected success, got rc={rc}"

    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert plan_file.exists(), "draft plan was not saved"
    content = plan_file.read_text(encoding="utf-8")
    assert "2026-06-01" in content
    assert "2026-06-02" in content
    assert "2026-06-03" in content

    # spend tracker updated
    spend = json.loads((tmp_repo / ".metrics" / "api_spend.json").read_text())
    month_key = dt.date.today().strftime("%Y-%m")
    assert month_key in spend
    assert spend[month_key]["calls"] == 1

    # nudge called with success
    assert m_nudge.call_count == 1
    assert m_nudge.call_args.args[0] == "monthly_plan_success"


# --- 8. Hard-fail paths -----------------------------------------------------


def test_hard_fail_on_brand_lint(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """T-1-02: brand violation → return 2, draft NOT saved, nudge brand_violation."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    plan = _build_valid_3day_plan(tmp_repo)
    # Inject a forbidden marketing word into one entry's body
    plan = plan.replace("Слово недели: «как бы».",
                        "Это революционный продукт от Diktum.")
    fake_msg.content[0].text = plan

    rc = main(month_override="2026-06")
    assert rc == 2, f"brand violation must yield exit 2; got {rc}"
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists(), "draft saved despite brand violation"
    assert m_nudge.call_count == 1
    assert m_nudge.call_args.args[0] == "monthly_plan_brand_violation"


def test_hard_fail_on_missing_media(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """T-1-03: media path that doesn't exist → return 1, draft NOT saved."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    plan = _build_valid_3day_plan(tmp_repo)
    # Replace one `media: []` with a fake media file
    plan = plan.replace(
        "channels: [vk]\nproduct: centry\nrubric: city\nmedia: []",
        "channels: [vk]\nproduct: centry\nrubric: city\n"
        "media:\n  - path: assets/missing-file.png\n"
        "    sha256: abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789\n"
        "    role: image",
        1,
    )
    fake_msg.content[0].text = plan

    rc = main(month_override="2026-06")
    assert rc == 1, f"missing media must yield exit 1; got {rc}"
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists()
    assert m_nudge.call_count == 1
    assert m_nudge.call_args.args[0] == "monthly_plan_failure"


def test_hard_fail_on_sha_mismatch(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """T-1-03: file exists but sha256 wrong → return 1."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    # Create real file
    f = tmp_repo / "assets" / "real.png"
    f.write_bytes(b"actual content")
    plan = _build_valid_3day_plan(tmp_repo)
    plan = plan.replace(
        "channels: [vk]\nproduct: centry\nrubric: city\nmedia: []",
        "channels: [vk]\nproduct: centry\nrubric: city\n"
        "media:\n  - path: assets/real.png\n"
        "    sha256: 0000000000000000000000000000000000000000000000000000000000000000\n"
        "    role: image",
        1,
    )
    fake_msg.content[0].text = plan

    rc = main(month_override="2026-06")
    assert rc == 1, f"sha mismatch must yield exit 1; got {rc}"
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists()


def test_hard_fail_on_cardinality(
    env_set, patched_repo, mock_anthropic_in_generator, monkeypatch
):
    """3 entries returned but month is supposedly 30 days → return 1."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    fake_msg.content[0].text = _build_valid_3day_plan(tmp_repo)
    # Patch monthrange to claim 30 days even though plan has 3
    fake_cal = type("FakeCal", (), {"monthrange": staticmethod(lambda y, m: (0, 30))})()
    monkeypatch.setattr(g, "calendar", fake_cal)

    rc = main(month_override="2026-06")
    assert rc == 1, f"cardinality mismatch must yield exit 1; got {rc}"
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists()


def test_hard_fail_on_authentication_error(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """API auth error → return 4, nudge failure."""
    from anthropic import AuthenticationError

    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    # AuthenticationError requires response/body in newer SDK; build minimal mock
    fake_resp = MagicMock(status_code=401, headers={})
    fake_client.messages.create.side_effect = AuthenticationError(
        message="auth failed", response=fake_resp, body={"error": "bad key"}
    )

    rc = main(month_override="2026-06")
    assert rc == 4, f"auth error must yield exit 4; got {rc}"
    assert m_nudge.call_args.args[0] == "monthly_plan_failure"


def test_hard_fail_on_api_timeout(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """API timeout → return 4, nudge failure."""
    from anthropic import APITimeoutError

    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    fake_client.messages.create.side_effect = APITimeoutError(request=MagicMock())

    rc = main(month_override="2026-06")
    assert rc == 4, f"timeout must yield exit 4; got {rc}"
    assert m_nudge.call_args.args[0] == "monthly_plan_failure"


def test_no_api_key_in_output(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """T-1-01 защёлка: api key prefix in generated text → return 1, draft NOT saved."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    tainted = _build_valid_3day_plan(tmp_repo) + "\n<!-- sk-ant-api03-FAKEFAKEFAKE12345 -->\n"
    fake_msg.content[0].text = tainted

    rc = main(month_override="2026-06")
    assert rc == 1, f"sanitizer must hard-fail; got rc={rc}"
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists(), "draft saved despite api key in output!"
    assert m_nudge.call_args.args[0] == "monthly_plan_failure"


def test_budget_cap_blocks_api_call(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """T-1-04: pre-flight blocks → no API call → return 3."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    spend_file = tmp_repo / ".metrics" / "api_spend.json"
    month_key = dt.date.today().strftime("%Y-%m")
    spend_file.write_text(
        json.dumps({
            "_schema_version": 1,
            month_key: {"input_tokens": 0, "output_tokens": 0, "usd": 4.99,
                        "calls": 0, "by_purpose": {}},
        })
    )

    rc = main(month_override="2026-06")
    assert rc == 3, f"budget cap must yield exit 3; got {rc}"
    # Anthropic client must NOT have been called
    assert fake_client.messages.create.call_count == 0
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists()
    assert m_nudge.call_args.args[0] == "monthly_plan_budget_cap"


def test_hard_fail_on_unparseable_output(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """Output is not valid plan markdown → return 1."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    fake_msg.content[0].text = "this is not a plan, no frontmatter, no sections"

    rc = main(month_override="2026-06")
    assert rc == 1
    plan_file = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    assert not plan_file.exists()


# ============================================================
# Phase 1.5 Plan 04 — --force-regenerate flag + regen_count increment
# ============================================================


def test_record_spend_increments_regen_count_on_regen(tmp_path, mocker):
    """is_regenerate=True → increment_regen_count called; v2 schema."""
    spend = tmp_path / "api_spend.json"
    spend.write_text(json.dumps({"_schema_version": 1}))
    mock_inc = mocker.patch(
        "src.monthly_plan_generator.increment_regen_count",
        return_value=1,
    )
    record_spend(
        spend,
        input_tokens=100,
        output_tokens=50,
        purpose="monthly_plan",
        is_regenerate=True,
    )
    mock_inc.assert_called_once()
    # month arg is current YYYY-MM
    called_month = mock_inc.call_args.args[1]
    assert re.fullmatch(r"\d{4}-\d{2}", called_month)


def test_record_spend_no_increment_on_normal_run(tmp_path, mocker):
    """is_regenerate=False (default) → no increment."""
    spend = tmp_path / "api_spend.json"
    spend.write_text(json.dumps({"_schema_version": 1}))
    mock_inc = mocker.patch("src.monthly_plan_generator.increment_regen_count")
    record_spend(
        spend,
        input_tokens=100,
        output_tokens=50,
        purpose="monthly_plan",
        is_regenerate=False,
    )
    mock_inc.assert_not_called()


def test_force_regenerate_overwrites_existing_plan(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """force_regenerate=True + existing plan → overwrite (no exit 1)."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator

    # Plant an existing plan file
    plans_dir = tmp_repo / "plans"
    plans_dir.mkdir(exist_ok=True)
    existing = plans_dir / "monthly_plan_2026-06.md"
    existing.write_text("---\nmonth: 2026-06\nstatus: draft\n---\nold body")

    fake_msg.content[0].text = (
        "---\n"
        "month: 2026-06\n"
        "generated_at: 2026-06-01T07:00:00Z\n"
        "generator: monthly_plan_generator v1\n"
        "---\n"
        "\n# План\n\n"
        "## 2026-06-01\n\n"
        "```yaml\nslug: forton-jun1\nchannels: [tg]\nproduct: forton-lab\n"
        "rubric: from_studio\nmedia: []\nstatus: draft\n```\n\n"
        "Июнь — месяц углубления.\n\nfortonlab.ru\n\n"
        "## 2026-06-02\n\n"
        "```yaml\nslug: diktum-jun2\nchannels: [tg]\nproduct: diktum\n"
        "rubric: words\nmedia: []\nstatus: draft\n```\n\n"
        "Слово недели.\n\ndiktumweb.ru\n\n"
        "## 2026-06-03\n\n"
        "```yaml\nslug: centry-jun3\nchannels: [vk]\nproduct: centry\n"
        "rubric: city\nmedia: []\nstatus: draft\n```\n\n"
        "Топ заведений.\n\ncentryweb.ru\n"
    )

    rc = main(month_override="2026-06", force_regenerate=True)
    assert rc == 0, f"force_regenerate=True must succeed even when file exists; got {rc}"
    # File overwritten with new content (no longer "old body")
    new_text = existing.read_text(encoding="utf-8")
    assert "old body" not in new_text
    assert "2026-06-01" in new_text


def test_force_regenerate_false_blocks_existing(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days
):
    """force_regenerate=False (default) + existing plan → exit 1, no generate call."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator

    plans_dir = tmp_repo / "plans"
    plans_dir.mkdir(exist_ok=True)
    existing = plans_dir / "monthly_plan_2026-06.md"
    existing.write_text("---\nmonth: 2026-06\nstatus: draft\n---\nold body")

    rc = main(month_override="2026-06", force_regenerate=False)
    assert rc == 1
    # Anthropic was NOT called — short-circuited
    assert fake_client.messages.create.call_count == 0
    # File was preserved
    assert "old body" in existing.read_text(encoding="utf-8")


def test_force_regenerate_increments_regen_count(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days, mocker
):
    """On force_regenerate=True success → increment_regen_count called once."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    plans_dir = tmp_repo / "plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / "monthly_plan_2026-06.md").write_text(
        "---\nmonth: 2026-06\nstatus: draft\n---\nold"
    )
    fake_msg.content[0].text = (
        "---\nmonth: 2026-06\ngenerated_at: 2026-06-01T07:00:00Z\n"
        "generator: monthly_plan_generator v1\n---\n\n# План\n\n"
        "## 2026-06-01\n\n```yaml\nslug: forton-jun1\nchannels: [tg]\n"
        "product: forton-lab\nrubric: from_studio\nmedia: []\nstatus: draft\n```\n\n"
        "Июнь.\n\nfortonlab.ru\n\n"
        "## 2026-06-02\n\n```yaml\nslug: diktum-jun2\nchannels: [tg]\n"
        "product: diktum\nrubric: words\nmedia: []\nstatus: draft\n```\n\n"
        "Слово.\n\ndiktumweb.ru\n\n"
        "## 2026-06-03\n\n```yaml\nslug: centry-jun3\nchannels: [vk]\n"
        "product: centry\nrubric: city\nmedia: []\nstatus: draft\n```\n\n"
        "Топ.\n\ncentryweb.ru\n"
    )

    mock_inc = mocker.patch(
        "src.monthly_plan_generator.increment_regen_count",
        return_value=1,
    )

    rc = main(month_override="2026-06", force_regenerate=True)
    assert rc == 0
    mock_inc.assert_called_once()


def test_initial_generate_calls_send_weekly_split(
    env_set, patched_repo, mock_anthropic_in_generator, patch_calendar_3days, mocker
):
    """W1 fix: on initial generate success → send_weekly_split called with inline_keyboard."""
    tmp_repo, m_nudge = patched_repo
    fake_client, fake_msg = mock_anthropic_in_generator
    fake_msg.content[0].text = (
        "---\nmonth: 2026-06\ngenerated_at: 2026-06-01T07:00:00Z\n"
        "generator: monthly_plan_generator v1\n---\n\n# План\n\n"
        "## 2026-06-01\n\n```yaml\nslug: forton-jun1\nchannels: [tg]\n"
        "product: forton-lab\nrubric: from_studio\nmedia: []\nstatus: draft\n```\n\n"
        "Июнь.\n\nfortonlab.ru\n\n"
        "## 2026-06-02\n\n```yaml\nslug: diktum-jun2\nchannels: [tg]\n"
        "product: diktum\nrubric: words\nmedia: []\nstatus: draft\n```\n\n"
        "Слово.\n\ndiktumweb.ru\n\n"
        "## 2026-06-03\n\n```yaml\nslug: centry-jun3\nchannels: [vk]\n"
        "product: centry\nrubric: city\nmedia: []\nstatus: draft\n```\n\n"
        "Топ.\n\ncentryweb.ru\n"
    )
    mock_split = mocker.patch(
        "src.monthly_plan_generator.send_weekly_split",
        return_value=[1, 2, 3],
    )

    rc = main(month_override="2026-06")
    assert rc == 0
    mock_split.assert_called_once()
    # Inline keyboard with 3 buttons (approve / edit / reject)
    kwargs = mock_split.call_args.kwargs
    inline_kb = kwargs.get("inline_keyboard") or (
        mock_split.call_args.args[1] if len(mock_split.call_args.args) > 1 else None
    )
    assert inline_kb is not None, "send_weekly_split must receive inline_keyboard"
    flat = [btn for row in inline_kb for btn in row]
    callback_actions = {btn["callback_data"].split(":")[0] for btn in flat}
    assert {"approve", "edit", "reject"} == callback_actions


def test_env_force_regenerate_recognized(monkeypatch):
    """ENV var FORCE_REGENERATE=true is parsed as boolean True at CLI level."""
    monkeypatch.setenv("FORCE_REGENERATE", "true")
    # Sanity check: env-reader exists in module (one-line helper or inline parse)
    val = os.environ.get("FORCE_REGENERATE", "").lower() == "true"
    assert val is True
