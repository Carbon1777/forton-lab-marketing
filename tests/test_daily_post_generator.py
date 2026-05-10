"""Unit tests for daily_post_generator — Phase 2 GEN-01/GEN-03/PREV-03."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import frontmatter
import pytest

from src.daily_post_generator import (
    DAILY_GENERATOR_PURPOSE,
    DAILY_REGEN_PURPOSE,
    MAX_REGEN_PER_DRAFT,
    MAX_TOKENS_SINGLE_POST,
    SYSTEM_PROMPT_DAILY,
    SYSTEM_PROMPT_EDIT_REGEN,
    BrandViolationError,
    GenerationError,
    build_user_prompt,
    generate_one,
    regen_one,
)
from src.monthly_plan_generator import BudgetExceededError
from src.plan_reader import Media, Mismatch, PlanEntry


def _make_entry(slug="centry-jun15-morning", channels=("tg", "vk"),
                media=(), product="centry", rubric="city_picks",
                content="Утренняя подборка кафе.", date=None) -> PlanEntry:
    return PlanEntry(
        date=date or dt.date(2026, 6, 15),
        slug=slug,
        channels=list(channels),
        media=list(media),
        content=content,
        product=product,
        rubric=rubric,
        status="draft",
    )


@pytest.fixture
def mock_pipeline_ok():
    """Stub all heavyweight side-effects in generate_one — Anthropic, sha verify, lint, spend."""
    with patch("src.daily_post_generator.preflight_budget_check") as mp_budget, \
         patch("src.daily_post_generator.verify_media_sha256", return_value=[]) as mp_sha, \
         patch("src.daily_post_generator.make_client") as mp_client, \
         patch("src.daily_post_generator.generate", return_value=(
             "Утренняя подборка кафе с авторским декором. centryweb.ru",
             1200, 250,
         )) as mp_gen, \
         patch("src.daily_post_generator.sanitize_output") as mp_san, \
         patch("src.daily_post_generator.lint", return_value={}) as mp_lint, \
         patch("src.daily_post_generator.record_spend") as mp_spend:
        yield {
            "budget": mp_budget, "sha": mp_sha, "client": mp_client,
            "gen": mp_gen, "sanitize": mp_san, "lint": mp_lint,
            "spend": mp_spend,
        }


# ===================================================================
# generate_one
# ===================================================================

def test_generate_one_writes_draft_with_required_frontmatter(tmp_path, mock_pipeline_ok):
    entry = _make_entry(media=[
        Media(path="assets/centry/jun15.jpg", sha256="abc123", role="image"),
    ])
    spend_file = tmp_path / ".metrics" / "api_spend.json"
    drafts_dir = tmp_path / "drafts"

    path = generate_one(entry, tmp_path, spend_file, drafts_dir)

    assert path == drafts_dir / "centry-jun15-morning.md"
    assert path.exists()
    loaded = frontmatter.load(path)
    assert loaded.metadata["slug"] == "centry-jun15-morning"
    assert loaded.metadata["title"] == "centry-jun15-morning"
    assert loaded.metadata["status"] == "draft"
    assert loaded.metadata["daily_regen_count"] == 0
    assert loaded.metadata["channels"] == ["tg", "vk"]
    assert loaded.metadata["product"] == "centry"
    assert loaded.metadata["rubric"] == "city_picks"
    assert loaded.metadata["plan_date"] == "2026-06-15"
    assert loaded.metadata["image"] == "assets/centry/jun15.jpg"
    assert loaded.metadata.get("video") is None
    assert "centryweb.ru" in loaded.content
    # generated_at is ISO with tzinfo
    assert "T" in loaded.metadata["generated_at"]
    # spend recorded with right purpose
    mock_pipeline_ok["spend"].assert_called_once()
    call_kwargs = mock_pipeline_ok["spend"].call_args.kwargs
    assert call_kwargs.get("purpose") == DAILY_GENERATOR_PURPOSE


def test_generate_one_video_entry_populates_video_field(tmp_path, mock_pipeline_ok):
    entry = _make_entry(media=[
        Media(path="assets/diktum/jun15.mp4", sha256="def456", role="video"),
    ])
    spend_file = tmp_path / ".metrics" / "api_spend.json"
    path = generate_one(entry, tmp_path, spend_file, tmp_path / "drafts")
    loaded = frontmatter.load(path)
    assert loaded.metadata["video"] == "assets/diktum/jun15.mp4"
    assert loaded.metadata.get("image") is None


def test_generate_one_brand_lint_violation_raises(tmp_path, mock_pipeline_ok):
    # Override lint to return a violation
    mock_pipeline_ok["lint"].return_value = {
        "names": [MagicMock(word="Алексей")],
    }
    entry = _make_entry()
    with pytest.raises(BrandViolationError) as exc:
        generate_one(entry, tmp_path, tmp_path / ".metrics" / "spend.json",
                     tmp_path / "drafts")
    # No draft file written
    assert not (tmp_path / "drafts" / "centry-jun15-morning.md").exists()
    assert entry.slug in exc.value.violations


def test_generate_one_budget_cap_exceeded_raises(tmp_path, mock_pipeline_ok):
    mock_pipeline_ok["budget"].side_effect = BudgetExceededError("$5/мес cap hit")
    entry = _make_entry()
    with pytest.raises(BudgetExceededError):
        generate_one(entry, tmp_path, tmp_path / ".metrics" / "spend.json",
                     tmp_path / "drafts")
    # generate not called when budget pre-flight fails
    mock_pipeline_ok["gen"].assert_not_called()


def test_generate_one_claude_outage_raises_GenerationError(tmp_path, mock_pipeline_ok):
    mock_pipeline_ok["gen"].side_effect = RuntimeError("Anthropic 503")
    entry = _make_entry()
    with pytest.raises(GenerationError, match="Claude API call failed"):
        generate_one(entry, tmp_path, tmp_path / ".metrics" / "spend.json",
                     tmp_path / "drafts")


def test_generate_one_media_sha_mismatch_raises_GenerationError(tmp_path, mock_pipeline_ok):
    m = Media(path="assets/centry/jun15.jpg", sha256="abc123", role="image")
    mock_pipeline_ok["sha"].return_value = [
        Mismatch(media=m, actual_sha256=None, reason="missing"),
    ]
    entry = _make_entry(media=[m])
    with pytest.raises(GenerationError, match="media verification failed"):
        generate_one(entry, tmp_path, tmp_path / ".metrics" / "spend.json",
                     tmp_path / "drafts")
    # Claude not called when sha mismatch
    mock_pipeline_ok["gen"].assert_not_called()


def test_generate_one_records_spend_with_token_counts(tmp_path, mock_pipeline_ok):
    """record_spend gets the input/output token counts returned by `generate`."""
    entry = _make_entry()
    generate_one(entry, tmp_path, tmp_path / ".metrics" / "spend.json",
                 tmp_path / "drafts")
    args, kwargs = mock_pipeline_ok["spend"].call_args
    # mock returned (text, 1200, 250) — positional in_tok=1200, out_tok=250
    assert args[1] == 1200
    assert args[2] == 250


# ===================================================================
# System prompt + user prompt
# ===================================================================

def test_system_prompt_includes_channel_limits():
    """GEN-03 invariant: SYSTEM_PROMPT_DAILY mentions all channel length limits."""
    assert "1024" in SYSTEM_PROMPT_DAILY    # TG caption
    assert "16000" in SYSTEM_PROMPT_DAILY   # VK
    assert "5000" in SYSTEM_PROMPT_DAILY    # YT description


def test_system_prompt_forbids_brand_violations():
    """SYSTEM_PROMPT_DAILY explicitly bans brand stop-list (defense-in-depth before lint)."""
    for word in ("Алексей", "Carbon", "Flutter", "Supabase", "Claude"):
        assert word in SYSTEM_PROMPT_DAILY


def test_build_user_prompt_includes_entry_fields():
    entry = _make_entry()
    prompt = build_user_prompt(entry)
    assert "centry-jun15-morning" in prompt
    assert "2026-06-15" in prompt
    assert "centry" in prompt
    assert "city_picks" in prompt
    assert "tg, vk" in prompt
    assert "Утренняя подборка кафе." in prompt


def test_build_user_prompt_handles_empty_media():
    entry = _make_entry(media=[])
    prompt = build_user_prompt(entry)
    assert "(без медиа)" in prompt


# ===================================================================
# regen_one
# ===================================================================

@pytest.fixture
def existing_draft(tmp_path):
    """Write an existing draft with daily_regen_count=0 for regen tests."""
    draft = frontmatter.Post(
        content="Старый текст. centryweb.ru",
        slug="centry-jun15-morning",
        channels=["tg", "vk"],
        product="centry",
        rubric="city_picks",
        generated_at="2026-06-15T09:00:00Z",
        status="draft",
        daily_regen_count=0,
    )
    path = tmp_path / "drafts" / "centry-jun15-morning.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(draft), encoding="utf-8")
    return path


def test_regen_one_increments_count(existing_draft, tmp_path):
    with patch("src.daily_post_generator.preflight_budget_check"), \
         patch("src.daily_post_generator.make_client"), \
         patch("src.daily_post_generator.generate", return_value=(
             "Новый текст после правки. centryweb.ru", 800, 400)), \
         patch("src.daily_post_generator.sanitize_output"), \
         patch("src.daily_post_generator.lint", return_value={}), \
         patch("src.daily_post_generator.record_spend"):
        spend_file = tmp_path / ".metrics" / "spend.json"
        path = regen_one(existing_draft, "убери последнее слово", spend_file)
        assert path == existing_draft
        loaded = frontmatter.load(path)
        assert loaded.metadata["daily_regen_count"] == 1
        assert "Новый текст" in loaded.content
        assert "last_edited_at" in loaded.metadata


def test_regen_one_cap_exceeded_raises(existing_draft, tmp_path):
    # Bump count to 3
    post = frontmatter.load(existing_draft)
    post.metadata["daily_regen_count"] = 3
    existing_draft.write_text(frontmatter.dumps(post), encoding="utf-8")

    with pytest.raises(GenerationError, match=r"regen limit \(3\) reached"):
        regen_one(existing_draft, "ещё одна правка",
                  tmp_path / ".metrics" / "spend.json")


def test_regen_one_brand_violation_raises_no_save(existing_draft, tmp_path):
    with patch("src.daily_post_generator.preflight_budget_check"), \
         patch("src.daily_post_generator.make_client"), \
         patch("src.daily_post_generator.generate", return_value=(
             "Текст с Алексеем", 800, 400)), \
         patch("src.daily_post_generator.sanitize_output"), \
         patch("src.daily_post_generator.lint", return_value={
             "names": [MagicMock(word="Алексей")]
         }):
        with pytest.raises(BrandViolationError):
            regen_one(existing_draft, "добавь Алексея",
                      tmp_path / ".metrics" / "spend.json")
        # Original body preserved (no save happened)
        loaded = frontmatter.load(existing_draft)
        assert "Старый текст" in loaded.content
        assert loaded.metadata["daily_regen_count"] == 0


def test_regen_one_atomic_write(existing_draft, tmp_path):
    """After successful regen file is parseable (atomic write — no half-writes)."""
    with patch("src.daily_post_generator.preflight_budget_check"), \
         patch("src.daily_post_generator.make_client"), \
         patch("src.daily_post_generator.generate", return_value=(
             "Новый текст после правки. centryweb.ru", 800, 400)), \
         patch("src.daily_post_generator.sanitize_output"), \
         patch("src.daily_post_generator.lint", return_value={}), \
         patch("src.daily_post_generator.record_spend"):
        regen_one(existing_draft, "test", tmp_path / ".metrics" / "spend.json")
    # Re-parse — would raise on half-written file
    loaded = frontmatter.load(existing_draft)
    assert loaded.metadata["daily_regen_count"] == 1


def test_regen_one_missing_draft_raises(tmp_path):
    nonexistent = tmp_path / "drafts" / "nope.md"
    with pytest.raises(GenerationError, match="draft not found"):
        regen_one(nonexistent, "x", tmp_path / ".metrics" / "spend.json")


def test_regen_one_records_spend_with_regen_purpose(existing_draft, tmp_path):
    with patch("src.daily_post_generator.preflight_budget_check"), \
         patch("src.daily_post_generator.make_client"), \
         patch("src.daily_post_generator.generate", return_value=(
             "Новый текст. centryweb.ru", 800, 400)), \
         patch("src.daily_post_generator.sanitize_output"), \
         patch("src.daily_post_generator.lint", return_value={}), \
         patch("src.daily_post_generator.record_spend") as mp_spend:
        regen_one(existing_draft, "test", tmp_path / ".metrics" / "spend.json")
        mp_spend.assert_called_once()
        assert mp_spend.call_args.kwargs.get("purpose") == DAILY_REGEN_PURPOSE


def test_regen_one_claude_outage_raises_GenerationError(existing_draft, tmp_path):
    with patch("src.daily_post_generator.preflight_budget_check"), \
         patch("src.daily_post_generator.make_client"), \
         patch("src.daily_post_generator.generate",
               side_effect=RuntimeError("Anthropic 503")), \
         patch("src.daily_post_generator.sanitize_output"):
        with pytest.raises(GenerationError, match="Claude regen failed"):
            regen_one(existing_draft, "x", tmp_path / ".metrics" / "spend.json")
