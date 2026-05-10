"""Spend tracker v2 helpers — adds regen_count tracking for Phase 1.5.

Schema v1 (Phase 1 — see monthly_plan_generator._load_spend):
    {"_schema_version": 1, "_updated": "...",
     "YYYY-MM": {input_tokens, output_tokens, usd, calls, by_purpose}}

Schema v2 (Phase 1.5 — additive, no migration):
    {"_schema_version": 2, "_updated": "...",
     "YYYY-MM": {... v1 fields ..., "regen_count": int},
     "regen_limit_per_month": int}

Schema v3 (Phase 7 — additive, no migration) — see 07-SCHEMA-v3.md:
    {"_schema_version": 3, "_updated": "...",
     "YYYY-MM": {... v1/v2 fields ...,
                 "by_provider": {<provider>: {"usd": float, "calls": int,
                                              [unit_field]: int}},
                 "by_day": {"YYYY-MM-DD": {"usd": float,
                                           "by_provider": {<provider>: float}}}},
     "regen_limit_per_month": int,
     "caps": {"monthly_abort_usd": float, "daily_usd": float,
              "by_provider_monthly_usd": {<provider>: float}}}

Backward-compat invariant:
    v1/v2 readers MUST treat absent by_provider/by_day/caps as missing (defaults).
    v3 writers MUST NOT touch v1/v2 fields (top-level usd, calls, input_tokens,
    output_tokens, by_purpose, regen_count) — those remain owned by
    monthly_plan_generator.record_spend / increment_regen_count.

Public API:
    v2 (Phase 1.5):
        DEFAULT_REGEN_LIMIT       -- constant, currently 3 (D-1.5-03)
        read_regen_count(path, m) -- safe getter, 0 on absent/corrupt/v1
        read_regen_limit(path)    -- safe getter, DEFAULT_REGEN_LIMIT fallback
        increment_regen_count(...) -- atomic write (tmp + os.replace), bumps to v2

    v3 (Phase 7 — BOOT-01 / BOOT-05):
        DEFAULT_DAILY_CAP_USD              -- $3.0 daily cap
        DEFAULT_MONTHLY_ABORT_USD          -- $15.0 monthly hard abort
        DEFAULT_PROVIDER_MONTHLY_CAPS      -- per-provider cap dict
        PROVIDER_UNIT_FIELDS               -- per-provider unit field name
        DailyCapExceededError              -- raised by preflight_check
        MonthlyAbortError                  -- raised by preflight_check
        ProviderMonthlyCapExceededError    -- raised by preflight_check
        read_provider_spend(path, m, p)    -- 0.0 on missing
        read_daily_spend(path, day)        -- 0.0 on missing
        read_monthly_total(path, m)        -- sum across providers OR v2 top-level
        preflight_check(...)               -- 3-layer cap enforcement
        record_provider_spend(...)         -- atomic v3 writer

Responsibility split:
    monthly_approval_bot (Plan 04) READS only.
    monthly_plan_generator (Plan 04 modification) WRITES via increment_regen_count
    when invoked with --force-regenerate.
    Phase 7+ callers (Replicate/ElevenLabs/LTX) WRITE via record_provider_spend
    and check budget via preflight_check.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Final, Literal

DEFAULT_REGEN_LIMIT: Final[int] = 3  # D-1.5-03 — 3 regenerate/month, $0.27 budget cap

# --- v3 (Phase 7) constants -------------------------------------------------

Provider = Literal["anthropic", "replicate", "elevenlabs", "ltx"]

DEFAULT_DAILY_CAP_USD: Final[float] = 3.0
DEFAULT_MONTHLY_ABORT_USD: Final[float] = 15.0
DEFAULT_PROVIDER_MONTHLY_CAPS: Final[dict[str, float]] = {
    "anthropic": 5.0,
    "replicate": 4.0,
    "elevenlabs": 5.0,
    "ltx": 6.0,
}
# unit_field convention per provider (07-SCHEMA-v3 §3.1)
PROVIDER_UNIT_FIELDS: Final[dict[str, str | None]] = {
    "anthropic": None,
    "replicate": "predict_seconds",
    "elevenlabs": "characters",
    "ltx": "seconds",
}


# --- v3 error classes -------------------------------------------------------

class DailyCapExceededError(RuntimeError):
    """Raised when (today_spend + est_cost) > caps.daily_usd."""


class MonthlyAbortError(RuntimeError):
    """Raised when (month_total + est_cost) >= caps.monthly_abort_usd."""


class ProviderMonthlyCapExceededError(RuntimeError):
    """Raised when (provider_month_spend + est_cost) > provider_cap."""


def _load(spend_file: Path) -> dict:
    """Load spend tracker JSON; return {} on absent/corrupt (with stderr warn)."""
    if not spend_file.exists():
        return {}
    try:
        return json.loads(spend_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(
            f"WARN: spend tracker {spend_file} unreadable ({exc!r}); "
            "treating as empty\n"
        )
        return {}


def read_regen_count(spend_file: Path, month: str) -> int:
    """Return regen_count[month] from spend tracker; 0 if absent or v1 schema.

    Args:
        spend_file: Path to .metrics/api_spend.json
        month: Month key in 'YYYY-MM' format (e.g. '2026-06')

    Returns:
        int regen_count for month, or 0 if:
            - file does not exist
            - file is corrupt JSON
            - month entry missing
            - regen_count field missing (v1 schema)
    """
    data = _load(spend_file)
    return int(data.get(month, {}).get("regen_count", 0))


def read_regen_limit(spend_file: Path, default: int = DEFAULT_REGEN_LIMIT) -> int:
    """Return regen_limit_per_month from tracker; `default` (3) if absent.

    Args:
        spend_file: Path to .metrics/api_spend.json
        default: Limit to use when field is absent (default DEFAULT_REGEN_LIMIT)

    Returns:
        int limit per month
    """
    data = _load(spend_file)
    return int(data.get("regen_limit_per_month", default))


def increment_regen_count(spend_file: Path, month: str) -> int:
    """Atomically increment regen_count[month] by 1; bump _schema_version to 2.

    Atomicity: writes to a sibling tmp-file then `os.replace` (POSIX atomic).
    If the process dies mid-write, the original file is preserved.

    Args:
        spend_file: Path to .metrics/api_spend.json (created if absent)
        month: Month key in 'YYYY-MM' format

    Returns:
        new regen_count value after increment

    Side effects:
        - Creates parent dirs if absent
        - Bumps _schema_version to 2
        - Updates _updated to current UTC ISO timestamp
        - Seeds regen_limit_per_month with DEFAULT_REGEN_LIMIT if absent
    """
    data = _load(spend_file)
    data["_schema_version"] = 2
    data["_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    entry = data.setdefault(month, {})
    entry["regen_count"] = int(entry.get("regen_count", 0)) + 1
    # Seed limit field if absent (visible for human inspection)
    data.setdefault("regen_limit_per_month", DEFAULT_REGEN_LIMIT)

    spend_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=spend_file.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, spend_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return entry["regen_count"]


# ============================================================================
# v3 (Phase 7) — multi-provider spend tracking + cap enforcement
# Authoritative contract: 07-SCHEMA-v3.md
# ============================================================================


def read_provider_spend(spend_file: Path, month: str, provider: str) -> float:
    """Cumulative USD for `provider` in `month`. 0.0 if missing.

    Args:
        spend_file: Path to .metrics/api_spend.json
        month: 'YYYY-MM'
        provider: provider key ('anthropic'/'replicate'/'elevenlabs'/'ltx'/...)

    Returns:
        float USD spent on this provider in this month; 0.0 if absent.
    """
    data = _load(spend_file)
    return float(
        data.get(month, {})
        .get("by_provider", {})
        .get(provider, {})
        .get("usd", 0.0)
    )


def read_daily_spend(spend_file: Path, day: str) -> float:
    """Cumulative USD across all providers for `day` ('YYYY-MM-DD'). 0.0 if missing."""
    month = day[:7]
    data = _load(spend_file)
    return float(
        data.get(month, {}).get("by_day", {}).get(day, {}).get("usd", 0.0)
    )


def read_monthly_total(spend_file: Path, month: str) -> float:
    """Cumulative USD across all providers in `month`.

    Uses `by_provider` sum if present (v3 shape); otherwise falls back to
    top-level v2 `usd` field. 0.0 if neither present.
    """
    data = _load(spend_file)
    entry = data.get(month, {})
    by_provider = entry.get("by_provider")
    if by_provider:
        return round(
            sum(float(p.get("usd", 0.0)) for p in by_provider.values()), 4
        )
    return float(entry.get("usd", 0.0))


def preflight_check(
    spend_file: Path,
    provider: str,
    est_cost_usd: float,
    *,
    daily_cap: float = DEFAULT_DAILY_CAP_USD,
    monthly_abort: float = DEFAULT_MONTHLY_ABORT_USD,
    provider_monthly_cap: float | None = None,
) -> None:
    """3-layer cap check for an upcoming spend.

    Failure order (narrowest cap first — fails fast on the most specific limit):
        1. Provider monthly cap → ProviderMonthlyCapExceededError
        2. Daily cap            → DailyCapExceededError
        3. Monthly abort        → MonthlyAbortError

    Each layer compares (current + est_cost_usd) against its cap. Daily/provider
    use strict greater-than; monthly_abort uses >= (hard catastrophic stop).

    Args:
        spend_file: Path to .metrics/api_spend.json (may be absent).
        provider: provider key (used to look up default cap in
                  DEFAULT_PROVIDER_MONTHLY_CAPS if `provider_monthly_cap=None`).
        est_cost_usd: estimated USD cost of the upcoming call.
        daily_cap: override daily cap (default $3.0).
        monthly_abort: override monthly abort (default $15.0).
        provider_monthly_cap: override per-provider cap. None → look up
                              DEFAULT_PROVIDER_MONTHLY_CAPS[provider]; if provider
                              is not in the dict, falls back to 100.0 (effectively
                              no extra constraint beyond daily/monthly).

    Raises:
        ProviderMonthlyCapExceededError: provider month + est > provider cap.
        DailyCapExceededError:           today + est > daily cap.
        MonthlyAbortError:               month total + est >= monthly abort.
    """
    today = dt.date.today().isoformat()
    month = today[:7]

    # 1. Provider monthly cap (narrowest — fail-fast)
    cap = (
        provider_monthly_cap
        if provider_monthly_cap is not None
        else DEFAULT_PROVIDER_MONTHLY_CAPS.get(provider, 100.0)
    )
    prov_spend = read_provider_spend(spend_file, month, provider)
    if prov_spend + est_cost_usd > cap:
        raise ProviderMonthlyCapExceededError(
            f"{provider} ${prov_spend:.4f}+${est_cost_usd:.4f}>${cap}"
        )

    # 2. Daily cap (sum across all providers today)
    today_spend = read_daily_spend(spend_file, today)
    if today_spend + est_cost_usd > daily_cap:
        raise DailyCapExceededError(
            f"daily ${today_spend:.4f}+${est_cost_usd:.4f}>${daily_cap}"
        )

    # 3. Monthly abort (catastrophic stop)
    month_total = read_monthly_total(spend_file, month)
    if month_total + est_cost_usd >= monthly_abort:
        raise MonthlyAbortError(
            f"monthly ${month_total:.4f}+${est_cost_usd:.4f}>=${monthly_abort}"
        )


def record_provider_spend(
    spend_file: Path,
    provider: str,
    usd: float,
    *,
    units: int = 0,
    unit_field: str | None = None,
) -> None:
    """Atomically record a provider spend.

    Increments (additive — never overwrites):
        - data[month].by_provider[provider].usd        (+= usd)
        - data[month].by_provider[provider].calls      (+= 1)
        - data[month].by_provider[provider][unit_field] (+= units, if both set)
        - data[month].by_day[today].usd                (+= usd)
        - data[month].by_day[today].by_provider[provider] (+= usd)

    Sets (overwrites):
        - data._schema_version = 3
        - data._updated = now (UTC ISO-8601)

    Does NOT touch (owned by monthly_plan_generator / increment_regen_count):
        - data[month].usd / .calls / .input_tokens / .output_tokens / .by_purpose
        - data[month].regen_count / data.regen_limit_per_month

    unit_field resolution:
        - If `unit_field` argument is given (and `units > 0`), uses it.
        - If `unit_field is None`, falls back to PROVIDER_UNIT_FIELDS.get(provider).
          When PROVIDER_UNIT_FIELDS[provider] is None (e.g. 'anthropic'), no unit
          field is written even if `units > 0`.

    Atomic write: tempfile.mkstemp + os.replace (POSIX atomic on same FS).
    On any I/O failure, the temp file is unlinked and the exception propagates;
    the original file is preserved untouched.

    Args:
        spend_file: Path to .metrics/api_spend.json (created if absent).
        provider: provider key ('anthropic'/'replicate'/'elevenlabs'/'ltx'/...).
        usd: USD spent on this call (float).
        units: provider-specific unit count (characters / predict_seconds / etc).
        unit_field: explicit unit field name. None → use PROVIDER_UNIT_FIELDS.
    """
    today = dt.date.today().isoformat()
    month = today[:7]
    data = _load(spend_file)
    data["_schema_version"] = 3
    data["_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()

    # Resolve unit_field (caller override, else per-provider default).
    effective_unit_field = (
        unit_field if unit_field is not None else PROVIDER_UNIT_FIELDS.get(provider)
    )

    entry = data.setdefault(month, {})
    bp = entry.setdefault("by_provider", {})
    pe = bp.setdefault(provider, {"usd": 0.0, "calls": 0})
    pe["usd"] = round(float(pe.get("usd", 0.0)) + float(usd), 4)
    pe["calls"] = int(pe.get("calls", 0)) + 1
    if effective_unit_field and units:
        pe[effective_unit_field] = int(pe.get(effective_unit_field, 0)) + int(units)

    bd = entry.setdefault("by_day", {})
    de = bd.setdefault(today, {"usd": 0.0, "by_provider": {}})
    de["usd"] = round(float(de.get("usd", 0.0)) + float(usd), 4)
    de_bp = de.setdefault("by_provider", {})
    de_bp[provider] = round(float(de_bp.get(provider, 0.0)) + float(usd), 4)

    spend_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=spend_file.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, spend_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
