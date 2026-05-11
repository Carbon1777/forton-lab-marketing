"""Stage 1 — Claude scripts video brief into structured JSON via tools API.

Single source of truth for the scriptwriter SYSTEM prompt:
``ai_talent/prompts/scriptwriter_system.md``. The same prompt is loaded by
``~/.claude/skills/ai-talent-scriptwriter/SKILL.md`` (interactive path).
This module is the headless CI/CD path used by ``assemble.py`` (Plan 06).

Pattern (BOOT-01 4-step):
    1. preflight_check("anthropic", est_cost_usd)
    2. client.messages.create(tools=[SCHEMA], tool_choice={...})
    3. validate + write script.json
    4. record_provider_spend("anthropic", usd=...)  # NO unit_field

Defense in depth for Phase 8 W-002 (Flux LoRA teeth artifact):
    L1 — SYSTEM prompt (scriptwriter_system.md) forbids the phrases.
    L2 — Python validator (this module) hard-fails on the phrases.
    L3 — SKILL.md post-substitution net (interactive path only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from anthropic import Anthropic

from src.spend_tracker_v2 import preflight_check, record_provider_spend

MODEL: Final[str] = "claude-sonnet-4-6"
MAX_TOKENS_SCRIPT: Final[int] = 4000
EST_COST_USD: Final[float] = 0.02
REQUEST_TIMEOUT_S: Final[float] = 60.0
MAX_RETRIES: Final[int] = 2

# Pattern S5 — resolve relative to module (issue #29).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
SYSTEM_PROMPT_PATH: Final[Path] = (
    _REPO_ROOT / "ai_talent" / "prompts" / "scriptwriter_system.md"
)

TEETH_BLACKLIST: Final[tuple[str, ...]] = (
    "laughing genuinely",
    "open mouth smile",
    "wide grin",
    "laughing with teeth visible",
)
TRIGGER_WORD: Final[str] = "OHWX_FORTONA"
REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "hook", "beats", "voice_lines", "cuts", "cta",
    "product", "series_flag", "hero_beat_id",
)

SCRIPT_SCHEMA: Final[dict] = {
    "name": "emit_video_script",
    "description": "Emit the structured script for a 9:16 AI-talent video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "hook": {"type": "string"},
            "beats": {
                "type": "array", "minItems": 4, "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "frame_prompt": {"type": "string"},
                        "duration_sec": {"type": "number"},
                        "is_hero": {"type": "boolean"},
                    },
                    "required": ["id", "frame_prompt", "duration_sec", "is_hero"],
                },
            },
            "voice_lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "beat_id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["beat_id", "text"],
                },
            },
            "cuts": {"type": "array", "items": {"type": "string"}},
            "cta": {"type": "string"},
            "product": {"type": "string", "enum": ["centry", "diktum"]},
            "series_flag": {"type": ["string", "null"]},
            "hero_beat_id": {"type": "string"},
        },
        "required": list(REQUIRED_KEYS),
    },
}


class ScriptBuilderError(RuntimeError):
    """Raised on schema validation, teeth blacklist, or Anthropic API errors."""


def _make_client() -> Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ScriptBuilderError("ANTHROPIC_API_KEY env var missing")
    return Anthropic(timeout=REQUEST_TIMEOUT_S, max_retries=MAX_RETRIES)


def validate_script(data: dict) -> None:
    """Hard-fail on missing keys, wrong hero count, teeth blacklist, or missing trigger.

    Raises ScriptBuilderError with a precise reason on first violation.
    Returns None on success.
    """
    if not isinstance(data, dict):
        raise ScriptBuilderError(f"expected dict, got {type(data).__name__}")
    for k in REQUIRED_KEYS:
        if k not in data:
            raise ScriptBuilderError(f"missing key '{k}' in script JSON")
    if not isinstance(data["beats"], list) or len(data["beats"]) < 4:
        raise ScriptBuilderError("beats must be a list of >=4 entries")

    heroes = [b for b in data["beats"] if b.get("is_hero")]
    if len(heroes) != 1:
        raise ScriptBuilderError(
            f"scenario B requires exactly 1 hero beat, got {len(heroes)}"
        )

    bad_trigger = [
        b.get("id") for b in data["beats"]
        if not b.get("frame_prompt", "").startswith(TRIGGER_WORD)
    ]
    if bad_trigger:
        raise ScriptBuilderError(
            f"frame_prompt must start with {TRIGGER_WORD}; "
            f"offending beats: {bad_trigger}"
        )

    for b in data["beats"]:
        low = b.get("frame_prompt", "").lower()
        for forbidden in TEETH_BLACKLIST:
            if forbidden in low:
                raise ScriptBuilderError(
                    f"teeth-artifact phrase forbidden in beat "
                    f"{b.get('id')!r}: {forbidden!r} (Phase 8 W-002 mitigation)"
                )

    beat_ids = {b["id"] for b in data["beats"]}
    if data["hero_beat_id"] not in beat_ids:
        raise ScriptBuilderError(
            f"hero_beat_id={data['hero_beat_id']!r} not in beat ids {sorted(beat_ids)}"
        )


def build_script(
    brief_md: str,
    character_card: str,
    out_path: Path,
    *,
    client: Anthropic | None = None,
    spend_file: Path = DEFAULT_SPEND_FILE,
    est_cost_usd: float = EST_COST_USD,
    system_prompt_path: Path = SYSTEM_PROMPT_PATH,
) -> Path:
    """Read brief + character card -> call Claude tools API -> write script.json.

    Returns ``out_path``. The dict written is schema-validated, teeth-blacklisted,
    and trigger-locked (``frame_prompt`` must start with OHWX_FORTONA in every beat).

    BOOT-01 invariant (asserted by ``test_ai_talent_BOOT_01_invariant``):
        preflight_check -> messages.create -> write file -> record_provider_spend.
    """
    if client is None:
        client = _make_client()
    if not system_prompt_path.exists():
        raise ScriptBuilderError(
            f"SYSTEM prompt missing: {system_prompt_path}"
        )
    system_prompt = system_prompt_path.read_text(encoding="utf-8")

    # STEP 1: preflight (BOOT-01)
    preflight_check(spend_file, "anthropic", est_cost_usd)

    # STEP 2: API call with FORCED tool_choice (T-11-05-01 mitigation)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS_SCRIPT,
        system=system_prompt,
        tools=[SCRIPT_SCHEMA],
        tool_choice={"type": "tool", "name": "emit_video_script"},
        messages=[{
            "role": "user",
            "content": f"BRIEF:\n{brief_md}\n\nCHARACTER:\n{character_card}",
        }],
    )

    tool_use_blocks = [
        b for b in msg.content if getattr(b, "type", None) == "tool_use"
    ]
    if not tool_use_blocks:
        raise ScriptBuilderError(
            f"no tool_use block in response; stop_reason={getattr(msg, 'stop_reason', None)}"
        )
    data: dict = dict(tool_use_blocks[0].input)

    # STEP 3: validate + persist (BEFORE recording spend)
    validate_script(data)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # STEP 4: record spend (BOOT-01). NOTE: NO unit_field for anthropic
    # (PROVIDER_UNIT_FIELDS["anthropic"] is None per spend_tracker_v2 contract).
    record_provider_spend(
        spend_file, "anthropic",
        usd=est_cost_usd,
    )
    return out_path


__all__ = [
    "ScriptBuilderError",
    "build_script",
    "validate_script",
    "SCRIPT_SCHEMA",
    "REQUIRED_KEYS",
    "TEETH_BLACKLIST",
    "TRIGGER_WORD",
    "SYSTEM_PROMPT_PATH",
    "MODEL",
    "EST_COST_USD",
]
