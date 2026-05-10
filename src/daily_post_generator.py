"""Daily post generator — Phase 2 GEN-01 / GEN-03 / PREV-03.

Generates the final post body for one plan entry per day, channel-aware
(TG caption ≤1024, VK ≤16000, YT description ≤5000), brand-safety enforced.

Reuses monthly_plan_generator API (Anthropic SDK + spend tracker) — no new
LLM client / budget logic. Brand-lint hard-fail before save (Phase 1 D-04).

Public API:
    generate_one(entry, repo_root, spend_file, drafts_dir) -> Path
    regen_one(draft_path, instruction, spend_file) -> Path
    BrandViolationError, GenerationError
    SYSTEM_PROMPT_DAILY, SYSTEM_PROMPT_EDIT_REGEN, build_user_prompt
    MAX_TOKENS_SINGLE_POST = 1500
    DAILY_GENERATOR_PURPOSE = "daily_post"
    DAILY_REGEN_PURPOSE = "daily_regen"
    MAX_REGEN_PER_DRAFT = 3

Threat-model anchors:
    T-2-04 — brand-lint hard-fail on every (re)generation BEFORE save
    T-2-05 — Anthropic SDK exceptions wrapped as GenerationError; caller
             (Plan 04 pre_flight) catches and emits tg_nudge alert
    T-2-04-A — MAX_REGEN_PER_DRAFT=3 cap проверяется ПЕРЕД budget pre-flight
    T-2-09 — verify_media_sha256 вызывается ПЕРВЫМ (cheap precondition)
    T-2-11 — atomic write через tmp + os.replace
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from typing import Final

import frontmatter

from src.brand_lint import lint
from src.monthly_plan_generator import (
    BudgetExceededError,
    estimate_call_cost,
    generate,
    make_client,
    preflight_budget_check,
    record_spend,
    sanitize_output,
)
from src.plan_reader import PlanEntry, verify_media_sha256

MAX_TOKENS_SINGLE_POST: Final[int] = 1500   # per Phase 0 D-19a budget cap
MAX_REGEN_PER_DRAFT: Final[int] = 3          # per D-2-02 cost cap
DAILY_GENERATOR_PURPOSE: Final[str] = "daily_post"
DAILY_REGEN_PURPOSE: Final[str] = "daily_regen"

# Ballpark for budget pre-flight: system + user prompt ≈ 2000 input tokens.
_ESTIMATED_PROMPT_TOKENS: Final[int] = 2000


SYSTEM_PROMPT_DAILY: Final[str] = """\
Ты — content-стратег студии Forton Lab. Тебе дан plan-entry (одна запись из
месячного плана) — заголовок, тематика, продукт, целевые каналы, медиа.

Твоя задача: написать ФИНАЛЬНЫЙ текст поста для всех целевых каналов сразу.

ОБЯЗАТЕЛЬНЫЕ требования (перенесены из Phase 1 generator):
1. Тон: дружелюбный, конкретный, без маркетингового шума.
2. ЗАПРЕЩЕНО упоминать: имена авторов (Алексей, Carbon, jcat), технологический
   стек (Flutter, Supabase, Claude, ChatGPT, Anthropic), штампованную лексику
   ("уникальный", "революционный", "прорывной", "лучший на рынке", "не имеет аналогов").
3. CTA — ОДНА ссылка на сайт продукта в конце (centryweb.ru/diktumweb.ru/fortonlab.ru).
4. Длина: пиши под самый строгий лимит из выбранных каналов:
   - tg/dzen: ≤ 1024 (TG caption limit, Дзен наследует через cross-post)
   - vk: ≤ 16000 (но если только vk — 800-1200 разумно)
   - yt: ≤ 5000 (description; для shorts заголовок ≤ 100)
5. Эмодзи 1-2 на пост, не парад.

ВЫХОД: только финальный текст поста (plain text, без frontmatter, без кодовых
оградок). Никаких комментариев перед/после. Текст пойдёт в caption поста как есть.
"""


SYSTEM_PROMPT_EDIT_REGEN: Final[str] = """\
Ты — content-стратег студии Forton Lab. Тебе дан существующий draft поста и
инструкция от юзера. Перепиши draft с учётом инструкции.

ОБЯЗАТЕЛЬНО:
1. Сохрани brand tone (Forton Lab — на «ты», без официоза, конкретно).
2. Длина — в пределах ±20% от оригинала (если юзер не просит явно сократить/расширить).
3. ЗАПРЕЩЁННЫЕ слова (попадание = брак): Алексей, Carbon, jcat, Flutter, Supabase,
   Claude, ChatGPT, GPT-4, Anthropic, Cursor, "уникальный", "революционный",
   "инновационный", "лучший на рынке", "не имеет аналогов", "прорывной".
4. Frontmatter не трогай — только текст body.
5. CTA — одна ссылка на сайт продукта в конце (если её нет в инструкции — оставь
   ту же что в оригинале).

ВЫХОД: только новый body в plain text. Никаких комментариев перед/после.
Никаких "вот переписанный текст:" — сразу с первого слова поста.
"""


USER_PROMPT_EDIT_REGEN_TPL: Final[str] = """\
=== СУЩЕСТВУЮЩИЙ DRAFT ===
{existing_body}

=== ИНСТРУКЦИЯ ОТ ЮЗЕРА ===
{user_instruction}

Перепиши draft с учётом инструкции.
"""


class BrandViolationError(Exception):
    """Output failed brand_lint hard-fail. Caller must show user (no save).

    ``violations`` shape is ``{slug: {category: [Violation, ...]}}`` — wrapped
    by ``generate_one`` / ``regen_one`` so the caller can attribute multiple
    failures to specific drafts in batch contexts.
    """

    def __init__(self, violations: dict):
        self.violations = violations
        words: set[str] = set()
        for value in violations.values():
            # Nested case: {category: [Violation, ...]}
            if isinstance(value, dict):
                for vs in value.values():
                    for v in vs:
                        w = getattr(v, "word", None)
                        if w:
                            words.add(str(w))
            # Flat case: [Violation, ...] — defensive
            elif isinstance(value, list):
                for v in value:
                    w = getattr(v, "word", None)
                    if w:
                        words.add(str(w))
        super().__init__(f"brand-lint hard-fail: {sorted(words)!r}")


class GenerationError(Exception):
    """Generic generation failure (Anthropic API outage, sanitize fail,
    media sha mismatch, regen cap reached). Caller decides whether to retry."""


def build_user_prompt(entry: PlanEntry) -> str:
    """Render plan-entry as user-prompt for SYSTEM_PROMPT_DAILY."""
    media_summary = "; ".join(
        f"{m.role}: {m.path}" for m in entry.media
    ) or "(без медиа)"
    return (
        f"Запись плана: {entry.slug}\n"
        f"Дата: {entry.date.isoformat()}\n"
        f"Продукт: {entry.product}\n"
        f"Рубрика: {entry.rubric}\n"
        f"Каналы: {', '.join(entry.channels)}\n"
        f"Медиа: {media_summary}\n"
        f"\n"
        f"Тема (черновик из плана):\n{entry.content}\n"
        f"\n"
        f"Сгенерируй финальный текст поста."
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic write: tmp file in same dir + os.replace. Defends against
    half-written drafts on crash mid-save (T-2-11)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def generate_one(entry: PlanEntry, repo_root: Path,
                 spend_file: Path, drafts_dir: Path) -> Path:
    """Generate final post body, brand-lint, save drafts/<slug>.md.

    Returns path to saved draft file.
    Raises:
        BudgetExceededError    — pre-flight cap hit
        BrandViolationError    — output failed brand-lint
        GenerationError        — Claude API outage / sanitize fail / media sha mismatch
    """
    # 1. Pre-flight budget (single post is small — 1500 tokens × $15/M = $0.0225 max output)
    est = estimate_call_cost(
        prompt_tokens=_ESTIMATED_PROMPT_TOKENS,
        max_tokens=MAX_TOKENS_SINGLE_POST,
    )
    preflight_budget_check(spend_file, est)

    # 2. Verify media sha256 BEFORE generation (defends MAJ-9; cheap precondition)
    try:
        mismatches = verify_media_sha256(entry, repo_root)
    except Exception as exc:
        raise GenerationError(
            f"media verification raised for {entry.slug}: {exc!r}"
        ) from exc
    if mismatches:
        raise GenerationError(
            f"media verification failed for {entry.slug}: "
            + "; ".join(
                f"{m.media.path}[{m.reason}]" for m in mismatches[:3]
            )
        )

    # 3. Claude API call (wraps Anthropic exceptions in GenerationError)
    try:
        client = make_client()
        text, in_tok, out_tok = generate(
            client, SYSTEM_PROMPT_DAILY, build_user_prompt(entry),
            MAX_TOKENS_SINGLE_POST,
        )
        sanitize_output(text)
    except BudgetExceededError:
        raise
    except Exception as exc:
        raise GenerationError(
            f"Claude API call failed for {entry.slug}: {exc!r}"
        ) from exc

    # 4. Brand-lint hard-fail (Phase 1 D-04 — lint only post.content)
    violations = lint(text)
    if violations:
        raise BrandViolationError({entry.slug: violations})

    # 5. Build draft frontmatter
    first_image = next(
        (m.path for m in entry.media if m.role == "image"), None
    )
    first_video = next(
        (m.path for m in entry.media if m.role == "video"), None
    )
    draft = frontmatter.Post(
        content=text,
        slug=entry.slug,
        title=entry.slug,   # mirror — для backward-compat с tg_post.py
        channels=list(entry.channels),
        product=entry.product,
        rubric=entry.rubric,
        media=[
            {"path": m.path, "sha256": m.sha256, "role": m.role}
            for m in entry.media
        ],
        image=first_image,
        video=first_video,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        status="draft",
        daily_regen_count=0,
        plan_date=entry.date.isoformat(),
    )
    draft_path = drafts_dir / f"{entry.slug}.md"
    _atomic_write_text(draft_path, frontmatter.dumps(draft))

    # 6. Record spend
    record_spend(spend_file, in_tok, out_tok, purpose=DAILY_GENERATOR_PURPOSE)
    return draft_path


def regen_one(draft_path: Path, edit_instruction: str,
              spend_file: Path) -> Path:
    """Regenerate draft body per user edit instruction (PREV-03).

    Updates draft_path in-place. Increments daily_regen_count.
    Raises:
        GenerationError       — regen cap (3) reached, or Claude API fail
        BrandViolationError   — output failed lint (caller prompts user retry)
        BudgetExceededError   — monthly $5 cap hit
    """
    if not draft_path.exists():
        raise GenerationError(f"draft not found: {draft_path}")

    draft = frontmatter.load(draft_path)
    cur_regen = int(draft.metadata.get("daily_regen_count", 0))
    if cur_regen >= MAX_REGEN_PER_DRAFT:
        raise GenerationError(
            f"regen limit ({MAX_REGEN_PER_DRAFT}) reached for {draft_path.name}"
        )

    # Pre-flight budget
    est = estimate_call_cost(
        prompt_tokens=_ESTIMATED_PROMPT_TOKENS,
        max_tokens=MAX_TOKENS_SINGLE_POST,
    )
    preflight_budget_check(spend_file, est)

    existing_body = draft.content
    user_prompt = USER_PROMPT_EDIT_REGEN_TPL.format(
        existing_body=existing_body,
        user_instruction=edit_instruction,
    )

    try:
        client = make_client()
        new_text, in_tok, out_tok = generate(
            client, SYSTEM_PROMPT_EDIT_REGEN, user_prompt,
            MAX_TOKENS_SINGLE_POST,
        )
        sanitize_output(new_text)
    except BudgetExceededError:
        raise
    except Exception as exc:
        raise GenerationError(
            f"Claude regen failed for {draft_path.name}: {exc!r}"
        ) from exc

    # Brand-lint hard-fail BEFORE save
    violations = lint(new_text)
    if violations:
        raise BrandViolationError(
            {draft.metadata.get("slug", draft_path.stem): violations}
        )

    # Atomic mutate + write back
    draft.content = new_text
    draft.metadata["daily_regen_count"] = cur_regen + 1
    draft.metadata["last_edited_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_write_text(draft_path, frontmatter.dumps(draft))

    record_spend(spend_file, in_tok, out_tok, purpose=DAILY_REGEN_PURPOSE)
    return draft_path
