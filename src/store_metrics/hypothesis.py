"""Claude Haiku 4.5 weekly insights — METRICS-09 (D-5-06).

Generates 1-3 short Russian-language insights (each ≤90 chars) from sparse
weekly numbers per product × store. Used ONLY when ``ANTHROPIC_API_KEY`` is
present in the environment; on any failure (budget cap, API error, parse
failure, brand violations) returns an empty list and the digest renders
without the «💡 Гипотезы недели» section. NEVER raises — the hard guarantee
is that ``generate()`` always returns a ``list[str]``.

Pipeline:
    1. ``_is_configured()`` gate     — skip if no API key
    2. ``_build_prompt(report)``     — system+user prompts from WeeklyReport
    3. ``spend_tracker_v2.preflight_check("anthropic", est_cost)``
                                       — fail-soft on cap exceeded
    4. ``Anthropic.messages.create`` — Haiku 4.5 call (max 500 output tokens)
    5. ``record_provider_spend``     — actual usage from ``response.usage``
    6. ``_parse_insights(text)``     — JSON parse + strip markdown code fences
    7. ``_filter_brand_violations``  — drop entries with brand_lint violations

Pricing (VERIFIED 2026-05-14, Anthropic pricing page):
    Input:  $1.00 per 1M tokens
    Output: $5.00 per 1M tokens
    Per-call estimate: ~1500 input + ~500 output = ~$0.004
    Monthly: 4 digests × $0.004 = $0.016/мес (vs $5 anthropic cap → запас 312×)

References:
    - .planning/phases/05-store-metrics/05-CONTEXT.md §«Claude API hypothesis»
    - .planning/phases/05-store-metrics/05-RESEARCH.md §«8. Claude Haiku 4.5»
    - .planning/phases/05-store-metrics/05-RESEARCH.md §«Pitfall 6» (markdown wrap)
    - src/monthly_plan_generator.py (Anthropic client pattern reuse)
    - src/spend_tracker_v2.py (v3 schema: preflight_check + record_provider_spend)
    - src/brand_lint.py (D-04 brand-safety check, applied per-insight)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Final

from .. import brand_lint
from ..spend_tracker_v2 import (
    DailyCapExceededError,
    MonthlyAbortError,
    ProviderMonthlyCapExceededError,
    preflight_check,
    record_provider_spend,
)
from .models import WeeklyReport


# ===================================================================
# Constants (per D-5-06 + RESEARCH §«8. Claude Haiku 4.5»)
# ===================================================================

MODEL: Final[str] = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS: Final[int] = 500     # hard cap per D-5-06
MAX_INSIGHTS: Final[int] = 3
MAX_INSIGHT_CHARS: Final[int] = 90
EST_COST_USD: Final[float] = 0.005      # preflight estimate; actual recorded post-call
REQUEST_TIMEOUT_S: Final[float] = 60.0  # Haiku is fast; short timeout per RESEARCH
MAX_RETRIES: Final[int] = 2

# Haiku 4.5 pricing (USD per 1M tokens) — VERIFIED 2026-05-14
INPUT_PRICE_PER_M: Final[float] = 1.0
OUTPUT_PRICE_PER_M: Final[float] = 5.0

# Default spend tracker location (caller may override via spend_file= kwarg).
# Relative path → resolved against CWD at call time. Caller (cli.py) is
# responsible for chdir to repo root before invoking generate().
_DEFAULT_SPEND_FILE: Final[Path] = Path(".metrics/api_spend.json")

# Regex to strip ```json ... ``` markdown code fences (RESEARCH Pitfall 6).
# Captures the JSON body between the fences; case-insensitive «json» tag.
_CODE_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)


# ===================================================================
# Configuration gate
# ===================================================================

def _is_configured() -> bool:
    """True iff ``ANTHROPIC_API_KEY`` env is present and non-empty."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ===================================================================
# Prompt construction
# ===================================================================

_SYSTEM_PROMPT: Final[str] = (
    "Ты аналитик метрик публикаций мобильных приложений студии Forton Lab.\n"
    "Анализируешь сухие цифры за неделю по 3 сторам × 2 продуктам.\n"
    "\n"
    "Твоя задача: дать 1-3 коротких инсайта на русском.\n"
    "- Каждый ≤90 знаков\n"
    "- Только наблюдения и вопросы («Diktum просел в GP — что-то с конверсией?»)\n"
    "- НЕ предлагай конкретных решений («что делать»)\n"
    "- Если ничего интересного — верни пустой массив\n"
    "\n"
    'ОБЯЗАТЕЛЬНЫЙ формат ответа — JSON:\n'
    '{"insights": ["текст1", "текст2"]}\n'
    "\n"
    "Не пиши ничего вне JSON. Не оборачивай в markdown code blocks."
)


def _extract_report_data(report: WeeklyReport) -> dict:
    """Build minimal bare-dict summary of report numbers for the LLM prompt.

    Extracts only the data needed for hypothesis generation — install counts,
    WoW deltas (current vs previous), and 4-week trend per product. Skips
    rating/geo/etc. to keep prompt compact.
    """
    products_data: list[dict] = []
    for prod in report.products:
        prev_by_store = {s.store: s for s in prod.prev_snapshots}
        stores_data: list[dict] = []
        for snap in prod.snapshots:
            prev = prev_by_store.get(snap.store)
            prev_installs = prev.installs if prev else None
            wow_pct: float | None = None
            if (
                snap.installs is not None
                and prev_installs is not None
                and prev_installs != 0
            ):
                wow_pct = round(
                    (snap.installs - prev_installs) / prev_installs * 100.0, 1
                )
            stores_data.append({
                "store": snap.store,
                "installs": snap.installs,
                "prev_installs": prev_installs,
                "wow_pct": wow_pct,
                "error": snap.error,
            })
        trend_4w = [
            {"week_start": p.week_start.isoformat(), "installs": p.installs}
            for p in prod.trend_4w
        ]
        products_data.append({
            "product": prod.product,
            "stores": stores_data,
            "trend_4w": trend_4w,
        })
    return {
        "week_start": report.week_start.isoformat(),
        "products": products_data,
    }


def _build_prompt(report: WeeklyReport) -> tuple[str, str]:
    """Compose (system, user) prompt pair for Haiku 4.5.

    System prompt is constant; user prompt is JSON-encoded report data.
    """
    data = _extract_report_data(report)
    user_msg = (
        "Цифры этой недели по 3 сторам × 2 продуктам:\n"
        "\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}\n"
        "\n"
        'Верни 1-3 коротких insight (≤90 знаков каждый) в JSON: '
        '{"insights": ["...", "..."]}'
    )
    return _SYSTEM_PROMPT, user_msg


# ===================================================================
# Haiku API call
# ===================================================================

def _call_haiku_raw(system: str, user: str, spend_file: Path) -> str:
    """Pre-flight + call + record spend. Returns raw response text.

    Raises:
        DailyCapExceededError / MonthlyAbortError / ProviderMonthlyCapExceededError
            from preflight_check — caller handles soft-fallback.
        anthropic.APIError (or subclass) on API failure.
        KeyError if ANTHROPIC_API_KEY suddenly missing (caller pre-checked via
            _is_configured but guard against race).

    Side effects:
        Atomically increments ``by_provider.anthropic.{usd,calls}`` in
        spend_file via ``record_provider_spend``.
    """
    # Pre-flight (raises on cap exceeded — caller catches)
    preflight_check(spend_file, provider="anthropic", est_cost_usd=EST_COST_USD)

    # Lazy import — avoid hard dependency for unconfigured environments
    from anthropic import Anthropic

    client = Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT_S,
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    # Compute actual cost from usage and record (spend tracker uses
    # `units=output_tokens` convention for Anthropic — unit_field=None per
    # PROVIDER_UNIT_FIELDS dict, so units arg is informational only).
    input_tokens = int(response.usage.input_tokens)
    output_tokens = int(response.usage.output_tokens)
    usd = (
        input_tokens / 1_000_000 * INPUT_PRICE_PER_M
        + output_tokens / 1_000_000 * OUTPUT_PRICE_PER_M
    )
    record_provider_spend(
        spend_file,
        provider="anthropic",
        usd=usd,
        units=output_tokens,
        unit_field="output_tokens",
    )

    # Extract text from first text block (Haiku returns single text block;
    # tool_use not requested).
    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    )
    return text


# ===================================================================
# Response parsing
# ===================================================================

def _strip_code_fence(text: str) -> str:
    """Strip surrounding ```json ... ``` if present (RESEARCH Pitfall 6).

    Returns the inner JSON body, or the original text unchanged if no fence
    detected. Idempotent — safe to call on already-clean JSON.
    """
    stripped = text.strip()
    m = _CODE_FENCE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return stripped


def _parse_insights(text: str) -> list[str]:
    """Parse Haiku response text → validated list of insight strings.

    On any failure (malformed JSON, missing ``insights`` key, wrong shape)
    returns ``[]`` and logs to stderr — never raises.

    Validation:
        - JSON parses to dict with ``insights`` key
        - Each entry is a non-empty string
        - Long entries truncated to MAX_INSIGHT_CHARS (90)
        - Caps total at MAX_INSIGHTS (3)
    """
    body = _strip_code_fence(text)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"hypothesis: JSON parse failed ({exc!r}); "
            f"body[:100]={body[:100]!r}\n"
        )
        return []

    if not isinstance(parsed, dict):
        sys.stderr.write(
            f"hypothesis: response not a dict (type={type(parsed).__name__})\n"
        )
        return []

    raw_insights = parsed.get("insights")
    if not isinstance(raw_insights, list):
        sys.stderr.write(
            f"hypothesis: 'insights' missing or not a list "
            f"(type={type(raw_insights).__name__})\n"
        )
        return []

    out: list[str] = []
    for entry in raw_insights:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        if not cleaned:
            continue
        if len(cleaned) > MAX_INSIGHT_CHARS:
            cleaned = cleaned[:MAX_INSIGHT_CHARS]
        out.append(cleaned)
        if len(out) >= MAX_INSIGHTS:
            break
    return out


# ===================================================================
# Brand-safety filter
# ===================================================================

def _filter_brand_violations(insights: list[str]) -> list[str]:
    """Drop insights with brand_lint violations (D-04 carry-forward).

    Per RESEARCH §«Brand-lint integration»: Haiku output lands in the public
    «Планировщик» TG channel. Stop-list applies (`Алексей`, `Claude`,
    `Flutter`, `Supabase`, marketing fluff). Hard-fail per-insight → drop
    that single insight, keep the rest. Empty list IS valid output.
    """
    safe: list[str] = []
    for ins in insights:
        violations = brand_lint.lint(ins)
        if violations:
            sys.stderr.write(
                f"hypothesis: DROPPED insight (brand violation: "
                f"{[v.word for v in violations]}): {ins[:60]!r}\n"
            )
            continue
        safe.append(ins)
    return safe


# ===================================================================
# Public entry point
# ===================================================================

def generate(
    report: WeeklyReport,
    spend_file: Path | None = None,
) -> list[str]:
    """Public entry — generate 1-3 brand-clean insights for the weekly digest.

    Returns ``[]`` and logs to stderr in any of these soft-fail cases:
        - ANTHROPIC_API_KEY not set (no API call attempted)
        - spend tracker cap exceeded (preflight raises)
        - Anthropic API error (network / 5xx / auth)
        - response JSON malformed / missing insights key
        - all insights filtered out by brand_lint
        - any other unexpected exception

    HARD GUARANTEE: this function never raises. The digest pipeline depends
    on graceful degradation — METRICS-09 is informational, not blocking.

    Args:
        report: WeeklyReport with snapshots/prev_snapshots/trend_4w data.
        spend_file: Path to spend tracker JSON. Defaults to
            ``.metrics/api_spend.json`` (relative — caller controls CWD).

    Returns:
        List of 0..MAX_INSIGHTS (3) insight strings, each ≤MAX_INSIGHT_CHARS
        (90) chars. Returns empty list on any failure path.
    """
    if spend_file is None:
        spend_file = _DEFAULT_SPEND_FILE

    if not _is_configured():
        sys.stderr.write(
            "hypothesis: ANTHROPIC_API_KEY not set — skipping LLM insights\n"
        )
        return []

    try:
        system, user = _build_prompt(report)
        raw_text = _call_haiku_raw(system, user, spend_file)
        insights = _parse_insights(raw_text)
        safe_insights = _filter_brand_violations(insights)
        return safe_insights
    except (
        DailyCapExceededError,
        MonthlyAbortError,
        ProviderMonthlyCapExceededError,
    ) as exc:
        sys.stderr.write(
            f"hypothesis: budget cap reached — {exc!r}; "
            f"digest sent without insights\n"
        )
        return []
    except Exception as exc:  # noqa: BLE001 — hard-guarantee: never raises
        # Catches: anthropic.APIError, anthropic.RateLimitError,
        # anthropic.APITimeoutError, anthropic.APIStatusError,
        # anthropic.AuthenticationError, ValueError, KeyError, etc.
        sys.stderr.write(
            f"hypothesis: unexpected error ({type(exc).__name__}: {exc!r}); "
            f"digest sent without insights\n"
        )
        return []
