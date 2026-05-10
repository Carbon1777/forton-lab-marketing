"""ElevenLabs tier gate — BOOT-02.

Single source of truth for "is the studio's ElevenLabs subscription paid".
Used by Phase 11 voice_client.py to deny voice generation on free tier.

Tier strings come from ElevenLabs API GET /v1/user/subscription
(verified RESEARCH.md RQ-2). Whitelist is conservative — trial/grant tiers
treated as NOT paid for v1; expand later if needed.
"""

from __future__ import annotations

import os
import sys
from typing import Final

# Verified против ElevenLabs API enum (RESEARCH RQ-2)
PAID_TIERS: Final[frozenset[str]] = frozenset({
    "starter",
    "creator",
    "pro",
    "growing_business",
    "scale_2024_08_10",
    "enterprise",
})

KNOWN_FREE_TIERS: Final[frozenset[str]] = frozenset({
    "free",
    "trial",
    "grant_tier_1_2025_07_23",
    "grant_tier_2_2025_07_23",
})

DEFAULT_TIER: Final[str] = "starter"


class TierMissingError(RuntimeError):
    """ELEVENLABS_TIER env var отсутствует или указывает на не-paid tier."""


def get_studio_tier(*, env_name: str = "ELEVENLABS_TIER", default: str = DEFAULT_TIER) -> str:
    """Read tier from env, lowercase + strip. Default = 'starter'.

    Per marketing-v3 convention — НЕ raise если env отсутствует, fallback на default.
    """
    raw = os.environ.get(env_name, "") or ""
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    return cleaned


def is_paid_tier(tier: str | None) -> bool:
    """Strict whitelist check. Unknown tier → False + stderr WARN."""
    if not tier:
        return False
    cleaned = tier.strip().lower()
    if not cleaned:
        return False
    if cleaned in PAID_TIERS:
        return True
    if cleaned in KNOWN_FREE_TIERS:
        return False
    # Unknown — log + deny (defensive default)
    sys.stderr.write(
        f"WARN: unknown ElevenLabs tier {cleaned!r}; "
        f"known paid={sorted(PAID_TIERS)}, known free={sorted(KNOWN_FREE_TIERS)}\n"
    )
    return False


def require_paid_tier() -> str:
    """Read tier from env + verify paid. Raises TierMissingError otherwise.

    For Phase 11 voice_client to call as gate.
    """
    tier = get_studio_tier()
    if not is_paid_tier(tier):
        raise TierMissingError(
            f"ElevenLabs tier {tier!r} is not paid. "
            f"Set ELEVENLABS_TIER env var to one of {sorted(PAID_TIERS)}."
        )
    return tier
