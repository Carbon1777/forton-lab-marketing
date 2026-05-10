"""Forton Lab — monthly content plan generator (Phase 1 PLAN-01).

Cron-driven (1st of month, 10:00 МСК via GitHub Actions). Reads strategy
(Brain), history (published/), media manifest (assets/+media/), calls
Claude Sonnet 4.5 API to generate ``marketing-v3/plans/monthly_plan_YYYY-MM.md``
in sectional Markdown format (variant b — see Phase 1 RESEARCH §«Plan File
Format Decision»).

Validates output through 4+1 gates BEFORE saving:

    1. sanitize_output  — no api key leak (T-1-01)
    2. parse            — sectional structure (PlanFormatError → exit 1)
    3. cardinality      — n_days_in_month entries exactly
    4. brand_lint       — every entry's body (T-1-02 → exit 2)
    5. media verify     — sha256 of every referenced file (T-1-03 → exit 1)

Environment:
    ANTHROPIC_API_KEY     — Claude API auth (must be in GH Secrets)
    TG_PLANNER_BOT_TOKEN  — for tg_nudge
    TG_OWNER_CHAT_ID      — for tg_nudge
    BRAIN_STRATEGY_PATH   — optional, path to content-plan-q2-2026.md
    MONTH_OVERRIDE        — optional, "YYYY-MM" to override current month

Exit codes:
    0 — success (plan saved, nudge sent)
    1 — internal error (parse/cardinality/media/sanitize; nudge sent)
    2 — brand-lint violations (draft NOT saved; nudge sent)
    3 — budget cap reached (no API call; nudge sent)
    4 — Anthropic API failure after SDK retries (nudge sent)
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import frontmatter
from anthropic import (
    Anthropic,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from src.brand_lint import lint, load_forbidden_words
from src.plan_reader import (
    Mismatch,
    Plan,
    PlanEntry,
    PlanFormatError,
    parse_plan_text,
    sha256_of_file,
    verify_media_sha256,
)
from src import tg_nudge


# ---------------------------------------------------------------------------
# Constants (per RESEARCH §«Cost & budget management»)
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-5"
# Per RESEARCH §«Pitfall 2 / Assumption A5»: monthly plan needs more headroom
# than the universal 1500-cap (which is for Phase 2 single-post regeneration).
# 8000 tokens × $15/M output = $0.12 per call — well within $5 monthly cap.
MAX_TOKENS_MONTHLY_PLAN = 8000

INPUT_PRICE_PER_M = 3.0   # USD per 1M input tokens (Claude Sonnet 4.5, May 2026)
OUTPUT_PRICE_PER_M = 15.0  # USD per 1M output tokens

MONTHLY_CAP_USD = 5.0
WARN_THRESHOLD_PCT = 0.6  # 60% — warn but don't block

REQUEST_TIMEOUT_S = 120.0
MAX_RETRIES = 2
MAX_HISTORY_FILES = 60

REPO_ROOT = Path(__file__).resolve().parent.parent  # marketing-v3/
DEFAULT_PLANS_DIR = REPO_ROOT / "plans"
DEFAULT_SPEND_FILE = REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_PUBLISHED_DIR = REPO_ROOT / "published"

# Sanitizer regex (T-1-01) — Anthropic API keys begin with sk-ant- followed by
# Base64-like body. We require ≥10 chars after the prefix to avoid false-positive
# matches on incidental "sk-ant-" substrings.
_API_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT (per RESEARCH §«Generator prompt strategy → System prompt»)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Ты — content-стратег студии Forton Lab. Студия делает 2 мобильных приложения:
- Centry — социальный планировщик встреч (голосование куда пойти)
- Diktum — AI-тренер русской речи и симулятор интервью с STAR

Твоя задача: сгенерировать месячный план публикаций в каналы студии (TG, VK,
YouTube, Дзен) — по одной записи на каждый день месяца.

ОБЯЗАТЕЛЬНЫЕ требования к каждой записи:
1. Тон: дружелюбный, конкретный, без маркетингового шума.
2. ЗАПРЕЩЕНО упоминать (это сорвёт публикацию):
   - Имена авторов (Алексей, Carbon, jcat)
   - Технологический стек (Flutter, Supabase, Claude, ChatGPT, GPT-4, Anthropic, OpenAI, Cursor)
   - Заштампованную лексику ("уникальный", "революционный", "инновационный",
     "лучший на рынке", "номер 1", "не имеет аналогов", "прорывной", "качественно новый")
3. CTA — ссылка на сайт продукта (centryweb.ru, diktumweb.ru, fortonlab.ru) —
   ОДНА ссылка на пост, в конце.
4. Длина: TG ≤ 1024 символов (TG caption limit), VK ≤ 16000, Дзен ≤ 5000.
   Если запись для нескольких каналов — пиши под самый строгий лимит.
5. Эмодзи — допустимы, но 1-2 на пост, не парад.

ФОРМАТ OUTPUT — ровно один Markdown документ с:
- Верхним YAML frontmatter (метаданные месяца)
- Секциями `## YYYY-MM-DD` с fenced ```yaml блоком метаданных и текстом поста
- См. пример ниже.

Пример одной секции:

## 2026-06-03

```yaml
slug: centry-jun3-piter
channels: [vk, dzen]
product: centry
rubric: city_picks
media:
  - path: marketing-v3/assets/jun3-piter-collage.png
    sha256: <SHA256_FROM_MANIFEST>
    role: image
status: draft
```

Топ-7 заведений в Питере для компании 4-6 человек по версии Centry. Бары,
кафе, нестандартные форматы.

centryweb.ru
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GenerationError(Exception):
    """Wrapper for any API/internal errors during generation."""


class BudgetExceededError(Exception):
    """Pre-flight budget cap reached — no API call should be made."""


class BrandViolationError(Exception):
    """Brand-safety lint hard-fail. Carries structured violations dict."""

    def __init__(self, violations: dict):
        self.violations = violations
        super().__init__(f"brand violations across {len(violations)} entries")


# ---------------------------------------------------------------------------
# SDK client factory
# ---------------------------------------------------------------------------


def make_client() -> Anthropic:
    """Construct an Anthropic SDK client with project defaults.

    Reads ANTHROPIC_API_KEY from env. Per RESEARCH §«Минимальный init pattern»:
    timeout=120s, max_retries=2 (SDK handles 429/5xx with exponential backoff).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise GenerationError("ANTHROPIC_API_KEY env var is missing")
    return Anthropic(
        api_key=api_key,
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT_S,
    )


# ---------------------------------------------------------------------------
# Cost & budget
# ---------------------------------------------------------------------------


def estimate_call_cost(prompt_tokens: int, max_tokens: int) -> float:
    """Pessimistic cost estimate: input by tokens, output assumes max_tokens fully used."""
    return (
        prompt_tokens / 1_000_000 * INPUT_PRICE_PER_M
        + max_tokens / 1_000_000 * OUTPUT_PRICE_PER_M
    )


def _load_spend(spend_file: Path) -> dict:
    """Safe JSON load — on JSONDecodeError or missing file returns default."""
    if not spend_file.exists():
        return {"_schema_version": 1, "_updated": None}
    try:
        return json.loads(spend_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(
            f"WARN: corrupt spend tracker ({spend_file}); resetting. Reason: {exc}\n"
        )
        return {"_schema_version": 1, "_updated": None}


def preflight_budget_check(spend_file: Path, est_cost: float) -> tuple[float, bool]:
    """Check if planned API call would exceed monthly cap.

    Returns:
        (current_usd, warn_at_60pct) — both informational.

    Raises:
        BudgetExceededError if current+est > MONTHLY_CAP_USD.
    """
    data = _load_spend(spend_file)
    month_key = dt.date.today().strftime("%Y-%m")
    entry = data.get(month_key, {"usd": 0.0})
    current = float(entry.get("usd", 0.0))
    if current + est_cost > MONTHLY_CAP_USD:
        raise BudgetExceededError(
            f"current ${current:.4f} + est ${est_cost:.4f} > cap ${MONTHLY_CAP_USD}"
        )
    warn = (current + est_cost) >= MONTHLY_CAP_USD * WARN_THRESHOLD_PCT
    return current, warn


def record_spend(
    spend_file: Path,
    input_tokens: int,
    output_tokens: int,
    purpose: str = "monthly_plan",
) -> None:
    """Update spend tracker atomically (tmp file + os.replace).

    Schema (per RESEARCH §«Spend tracker — детальный формат»):
        {"_schema_version": 1, "_updated": ISO,
         "YYYY-MM": {"input_tokens", "output_tokens", "usd", "calls",
                     "by_purpose": {purpose: {"calls", "usd"}}}}
    """
    data = _load_spend(spend_file)
    month_key = dt.date.today().strftime("%Y-%m")
    entry = data.setdefault(
        month_key,
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "usd": 0.0,
            "calls": 0,
            "by_purpose": {},
        },
    )
    # Defensive: if pre-existing entry was created without by_purpose (older schema)
    entry.setdefault("by_purpose", {})

    usd = (
        input_tokens / 1_000_000 * INPUT_PRICE_PER_M
        + output_tokens / 1_000_000 * OUTPUT_PRICE_PER_M
    )
    entry["input_tokens"] = entry.get("input_tokens", 0) + input_tokens
    entry["output_tokens"] = entry.get("output_tokens", 0) + output_tokens
    entry["usd"] = round(float(entry.get("usd", 0.0)) + usd, 4)
    entry["calls"] = entry.get("calls", 0) + 1

    pe = entry["by_purpose"].setdefault(purpose, {"calls": 0, "usd": 0.0})
    pe["calls"] = pe.get("calls", 0) + 1
    pe["usd"] = round(float(pe.get("usd", 0.0)) + usd, 4)

    data["_updated"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()

    spend_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = spend_file.with_suffix(spend_file.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, spend_file)


# ---------------------------------------------------------------------------
# Output sanitizer (T-1-01)
# ---------------------------------------------------------------------------


def sanitize_output(text: str) -> None:
    """Hard-fail if generated text contains an Anthropic API key prefix.

    Raises GenerationError on any match. Used as a safety net — Claude should
    never emit an api key, but if a prompt-injection attack ever made it
    through and tricked the model into echoing one, this catch prevents the
    leak from being committed to git.
    """
    m = _API_KEY_RE.search(text)
    if m:
        raise GenerationError(
            "output contains api-key-like substring "
            f"({m.group(0)[:12]}...); refusing to save"
        )


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
}


def _guess_mime(suffix: str) -> str:
    return _MIME_BY_SUFFIX.get(suffix.lower(), "application/octet-stream")


def build_media_manifest(repo_root: Path) -> str:
    """Recursive listing of assets/+media/ with path, sha256, size_kb, mime.

    Per RESEARCH §«Media manifest — структура». Skips dotfiles and unreadable
    files. Returns "(no media files yet)" if nothing found.
    """
    rows: list[str] = []
    for d in [repo_root / "assets", repo_root / "media"]:
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                sha = sha256_of_file(f)
            except OSError:
                continue
            size_kb = f.stat().st_size // 1024
            mime = _guess_mime(f.suffix)
            rel = f.relative_to(repo_root)
            rows.append(
                f"- path: {rel}\n"
                f"  sha256: {sha}\n"
                f"  size_kb: {size_kb}\n"
                f"  mime: {mime}"
            )
    return "\n".join(rows) if rows else "(no media files yet)"


def _load_history(published_dir: Path, max_files: int = MAX_HISTORY_FILES) -> str:
    """Last ``max_files`` .md files from published/, mtime-descending.

    Each file is rendered as: filename + frontmatter (compact JSON) + first
    200 chars of body. Per RESEARCH §«User prompt».
    """
    if not published_dir.exists():
        return "(no history yet)"
    files = sorted(
        (f for f in published_dir.glob("*.md") if not f.name.startswith("_")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_files]
    parts: list[str] = []
    for f in files:
        try:
            post = frontmatter.load(f)
            meta_yaml = json.dumps(dict(post.metadata), ensure_ascii=False, default=str)
            body_excerpt = post.content.strip()[:200]
            parts.append(f"### {f.name}\nmeta: {meta_yaml}\nbody: {body_excerpt}\n")
        except Exception as e:  # parse robustness — never fail prompt build
            parts.append(f"### {f.name} (parse error: {e!r})\n")
    return "\n".join(parts) if parts else "(no history yet)"


def _load_strategy(strategy_path: Path | None) -> str:
    """Load Brain content-plan-q2-2026.md if reachable, else fallback warning.

    Brain lives outside the repo (user's local KB). Tests don't have access,
    so missing-strategy must be handled gracefully — generator falls back to
    a generic instruction, and writes a warning to stderr.
    """
    if strategy_path is None or not strategy_path.exists():
        sys.stderr.write(
            "WARN: BRAIN_STRATEGY_PATH not set or unreachable; "
            "using generic fallback strategy\n"
        )
        return "(стратегия не указана — сгенерируй generic план по дням месяца)"
    try:
        return strategy_path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"WARN: cannot read strategy: {exc}; using fallback\n")
        return "(стратегия недоступна — сгенерируй generic план)"


def build_user_prompt(
    month: str,
    n_days: int,
    strategy: str,
    history: str,
    manifest: str,
) -> str:
    """Assemble the dynamic per-call prompt per RESEARCH §«User prompt»."""
    return (
        f"Сгенерируй план публикаций на МЕСЯЦ: {month}.\n"
        f"Дней в месяце: {n_days}.\n"
        f"Цель: {n_days} записей (ровно по одной на каждый день).\n"
        "\n"
        "=== СТРАТЕГИЯ КВАРТАЛА (источник тем) ===\n"
        f"{strategy}\n"
        "\n"
        "=== ИСТОРИЯ ПОСЛЕДНИХ ПОСТОВ (для контекста стиля и чтобы не повторяться) ===\n"
        f"{history}\n"
        "\n"
        "=== ДОСТУПНЫЕ МЕДИА (ассеты для прикрепления) ===\n"
        f"{manifest}\n"
        "\n"
        "=== ИНСТРУКЦИЯ ===\n"
        "1. Распредели темы из стратегии по дням месяца.\n"
        "2. Используй ТОЛЬКО существующие медиа-файлы из манифеста выше. Если для\n"
        "   записи медиа не нужно (например, чисто текстовый TG-пост) — `media: []`.\n"
        "3. Если медиа нужно — поставь `path` точно как в манифесте, и `sha256`\n"
        "   точно как в манифесте (НЕ выдумывай хэши).\n"
        "4. По дням без релевантных тем — генерируй короткие \"из жизни студии\" /\n"
        "   \"слово недели\" посты.\n"
        f"5. Все {n_days} записей должны быть в одном файле.\n"
        "\n"
        "ВЫХОД: только Markdown — никаких комментариев перед/после, никаких ```markdown\n"
        "обёрток. Начни сразу с `---` верхнего frontmatter.\n"
    )


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------


def generate(
    client: Anthropic,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    """Single API call. Returns (text, input_tokens, output_tokens).

    Raises GenerationError on any error condition (per RESEARCH §«Error
    handling таблица»). SDK handles 429/5xx retries internally — by the
    time we see RateLimitError, retries are exhausted.
    """
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except RateLimitError as e:
        raise GenerationError(f"rate limit exhausted after retries: {e}") from e
    except APITimeoutError as e:
        raise GenerationError(f"timeout >{REQUEST_TIMEOUT_S}s: {e}") from e
    except AuthenticationError as e:
        raise GenerationError(f"ANTHROPIC_API_KEY rejected: {e}") from e
    except APIStatusError as e:
        raise GenerationError(f"HTTP {e.status_code}: {e.message}") from e

    if getattr(resp, "stop_reason", None) == "max_tokens":
        sys.stderr.write(
            f"WARN: hit max_tokens cap ({max_tokens}); output may be truncated\n"
        )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    usage = resp.usage
    return text, int(usage.input_tokens), int(usage.output_tokens)


# ---------------------------------------------------------------------------
# Output validation (4+1 stage gate)
# ---------------------------------------------------------------------------


def validate_generated_plan(
    text: str,
    repo_root: Path,
    n_days: int,
    forbidden: list[str],
) -> Plan:
    """Run the 4+1 gates per RESEARCH §«Output validation» and return Plan.

    1. sanitize_output (T-1-01)
    2. parse_plan_text (PlanFormatError → re-raised as GenerationError)
    3. cardinality (n_days_in_month entries exactly)
    4. brand_lint each entry (BrandViolationError if any)
    5. media verify each entry (GenerationError if any mismatch)
    """
    sanitize_output(text)

    try:
        plan = parse_plan_text(text, Path("<generated>"))
    except PlanFormatError as exc:
        raise GenerationError(f"parse failed: {exc}") from exc

    if len(plan.entries) != n_days:
        raise GenerationError(
            f"cardinality mismatch: got {len(plan.entries)} entries, expected {n_days}"
        )

    # Brand-lint: ALWAYS use the default categorisation (reads
    # marketing-v3/.lint/forbidden_words.txt and applies per-category match
    # strategies — name=word-boundary, stack=word-boundary, marketing=substring).
    # Passing `words=forbidden` forces every entry into the "stack" category
    # (whole-word) and breaks substring root-matching for marketing fluff like
    # «революцион» → «революционный». The `forbidden` arg is kept in the
    # signature for future ad-hoc test injections but unused in production.
    _ = forbidden
    violations: dict[str, list] = {}
    for entry in plan.entries:
        v = lint(entry.content)
        if v:
            violations[entry.date.isoformat()] = v
    if violations:
        raise BrandViolationError(violations)

    media_problems: list[tuple[str, Mismatch]] = []
    for entry in plan.entries:
        mismatches = verify_media_sha256(entry, repo_root)
        for mm in mismatches:
            media_problems.append((entry.date.isoformat(), mm))
    if media_problems:
        summary = "; ".join(
            f"{d}: {mm.media.path} [{mm.reason}]" for d, mm in media_problems[:5]
        )
        raise GenerationError(f"media verification failed: {summary}")

    return plan


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic write via tmp + os.replace (POSIX-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# tg_nudge wrapper — never crashes the orchestrator
# ---------------------------------------------------------------------------


def _safe_nudge(template_key: str, **vars: Any) -> None:
    """Call tg_nudge.send but swallow exceptions — main() must still return correct exit."""
    try:
        tg_nudge.send(template_key, **vars)
    except Exception as exc:
        sys.stderr.write(f"WARN: tg_nudge {template_key!r} failed: {exc!r}\n")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _month_ru(month: str) -> str:
    """`2026-06` → `июня 2026`."""
    try:
        y, m = month.split("-")
        return f"{_RU_MONTHS[int(m)]} {y}"
    except (ValueError, KeyError):
        return month


def _format_violations(brand_violations: dict) -> str:
    """Render BrandViolationError.violations dict for nudge HTML."""
    lines: list[str] = []
    for date_iso, vlist in sorted(brand_violations.items()):
        for v in vlist:
            word = getattr(v, "word", str(v))
            line = getattr(v, "line", "?")
            lines.append(f"• «{word}» в записи на {date_iso} (line {line})")
    return "\n".join(lines) + "\n" if lines else "(нет деталей)\n"


def main(
    today: dt.date | None = None,
    month_override: str | None = None,
) -> int:
    """Orchestrate the full pipeline. Returns exit code (0..4)."""
    today = today or dt.date.today()
    month = month_override or today.strftime("%Y-%m")
    try:
        year_str, month_str = month.split("-")
        year, mnum = int(year_str), int(month_str)
    except ValueError:
        sys.stderr.write(f"ERROR: invalid month override: {month!r}\n")
        return 1
    n_days = calendar.monthrange(year, mnum)[1]
    month_ru = _month_ru(month)

    actions_url = "https://github.com/Carbon1777/forton-lab-marketing/actions"
    status_url = "https://status.anthropic.com"
    console_url = "https://console.anthropic.com/settings/limits"

    # --- 1. Pre-flight budget ----------------------------------------------
    est_cost = estimate_call_cost(prompt_tokens=10_000, max_tokens=MAX_TOKENS_MONTHLY_PLAN)
    spend_file = DEFAULT_SPEND_FILE
    try:
        current_usd, warn = preflight_budget_check(spend_file, est_cost)
    except BudgetExceededError as exc:
        sys.stderr.write(f"ERROR: budget cap reached: {exc}\n")
        # current usd = whatever was already in tracker
        try:
            data = _load_spend(spend_file)
            current_usd = float(data.get(today.strftime("%Y-%m"), {}).get("usd", 0.0))
        except Exception:
            current_usd = MONTHLY_CAP_USD
        _safe_nudge(
            "monthly_plan_budget_cap",
            month_ru=month_ru,
            usd_current=f"{current_usd:.2f}",
            usd_cap=f"{MONTHLY_CAP_USD:.2f}",
            console_url=console_url,
            actions_url=actions_url,
        )
        return 3

    if warn:
        sys.stderr.write(
            f"WARN: budget at {(current_usd + est_cost) / MONTHLY_CAP_USD:.0%} of cap "
            f"(${current_usd + est_cost:.2f} / ${MONTHLY_CAP_USD})\n"
        )

    # --- 2. Build prompts --------------------------------------------------
    try:
        strategy_env = os.environ.get("BRAIN_STRATEGY_PATH", "").strip()
        strategy_path = Path(strategy_env) if strategy_env else None
        strategy = _load_strategy(strategy_path)
        history = _load_history(DEFAULT_PUBLISHED_DIR)
        manifest = build_media_manifest(REPO_ROOT)
        user_prompt = build_user_prompt(month, n_days, strategy, history, manifest)
    except Exception as exc:
        sys.stderr.write(f"ERROR: prompt build failed: {exc!r}\n")
        _safe_nudge(
            "monthly_plan_failure",
            month_ru=month_ru,
            reason=f"prompt build error: {exc}",
            status_url=status_url,
            actions_url=actions_url,
        )
        return 1

    # --- 3. API call -------------------------------------------------------
    try:
        client = make_client()
        text, in_tok, out_tok = generate(client, SYSTEM_PROMPT, user_prompt,
                                         MAX_TOKENS_MONTHLY_PLAN)
    except GenerationError as exc:
        sys.stderr.write(f"ERROR: Anthropic call failed: {exc}\n")
        _safe_nudge(
            "monthly_plan_failure",
            month_ru=month_ru,
            reason=str(exc),
            status_url=status_url,
            actions_url=actions_url,
        )
        return 4

    # --- 4. Validate output (4+1 gates) ------------------------------------
    try:
        forbidden = load_forbidden_words()
    except OSError:
        forbidden = []  # falls back to brand_lint default — still works
    try:
        plan = validate_generated_plan(text, REPO_ROOT, n_days, forbidden)
    except BrandViolationError as exc:
        sys.stderr.write(f"ERROR: brand violations: {exc}\n")
        _safe_nudge(
            "monthly_plan_brand_violation",
            month_ru=month_ru,
            violations_list=_format_violations(exc.violations),
            actions_url=actions_url,
        )
        return 2
    except GenerationError as exc:
        sys.stderr.write(f"ERROR: validation failed: {exc}\n")
        _safe_nudge(
            "monthly_plan_failure",
            month_ru=month_ru,
            reason=f"validation error: {exc}",
            status_url=status_url,
            actions_url=actions_url,
        )
        return 1
    except Exception as exc:
        sys.stderr.write(f"ERROR: unexpected validation error: {exc!r}\n")
        _safe_nudge(
            "monthly_plan_failure",
            month_ru=month_ru,
            reason=f"unexpected error: {exc!r}",
            status_url=status_url,
            actions_url=actions_url,
        )
        return 1

    # --- 5. Save -----------------------------------------------------------
    plan_file = DEFAULT_PLANS_DIR / f"monthly_plan_{month}.md"
    try:
        _atomic_write_text(plan_file, text)
    except OSError as exc:
        sys.stderr.write(f"ERROR: save failed: {exc}\n")
        _safe_nudge(
            "monthly_plan_failure",
            month_ru=month_ru,
            reason=f"disk write failed: {exc}",
            status_url=status_url,
            actions_url=actions_url,
        )
        return 1

    # --- 6. Record spend ---------------------------------------------------
    try:
        record_spend(spend_file, in_tok, out_tok)
    except OSError as exc:
        sys.stderr.write(f"WARN: record_spend failed: {exc}\n")
        # Plan was already saved — don't fail the run

    # --- 7. Success nudge --------------------------------------------------
    plan_usd = (
        in_tok / 1_000_000 * INPUT_PRICE_PER_M
        + out_tok / 1_000_000 * OUTPUT_PRICE_PER_M
    )
    commit_url = os.environ.get(
        "PLAN_COMMIT_URL",
        "https://github.com/Carbon1777/forton-lab-marketing/commits/main",
    )
    commit_sha7 = os.environ.get("PLAN_COMMIT_SHA7", "n/a")
    _safe_nudge(
        "monthly_plan_success",
        month_ru=month_ru,
        plan_path=str(plan_file.relative_to(REPO_ROOT))
        if plan_file.is_absolute() else str(plan_file),
        commit_url=commit_url,
        commit_sha7=commit_sha7,
        entries_count=len(plan.entries),
        usd_spent=f"{plan_usd:.4f}",
    )
    sys.stderr.write(
        f"OK: saved {plan_file} ({len(plan.entries)} entries, ${plan_usd:.4f})\n"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    month_override = os.environ.get("MONTH_OVERRIDE", "").strip() or None
    sys.exit(main(month_override=month_override))
