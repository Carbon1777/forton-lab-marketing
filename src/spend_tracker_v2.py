"""Spend tracker v2 helpers — adds regen_count tracking for Phase 1.5.

Schema v1 (Phase 1 — see monthly_plan_generator._load_spend):
    {"_schema_version": 1, "_updated": "...",
     "YYYY-MM": {input_tokens, output_tokens, usd, calls, by_purpose}}

Schema v2 (Phase 1.5 — additive, no migration):
    {"_schema_version": 2, "_updated": "...",
     "YYYY-MM": {... v1 fields ..., "regen_count": int},
     "regen_limit_per_month": int}

Backward-compat invariant:
    v1 readers MUST treat absent regen_count as 0;
    absent regen_limit_per_month as DEFAULT_REGEN_LIMIT.

Public API (used by Plans 02 / 04):
    DEFAULT_REGEN_LIMIT       -- constant, currently 3 (D-1.5-03)
    read_regen_count(path, m) -- safe getter, 0 on absent/corrupt/v1
    read_regen_limit(path)    -- safe getter, DEFAULT_REGEN_LIMIT fallback
    increment_regen_count(...) -- atomic write (tmp + os.replace), bumps schema to v2

Responsibility split:
    monthly_approval_bot (Plan 04) READS only.
    monthly_plan_generator (Plan 04 modification) WRITES via increment_regen_count
    when invoked with --force-regenerate.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Final

DEFAULT_REGEN_LIMIT: Final[int] = 3  # D-1.5-03 — 3 regenerate/month, $0.27 budget cap


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
