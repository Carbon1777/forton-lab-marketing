"""BOOT-01 invariant: every external-API stage source has preflight_check
BEFORE the API call AND record_provider_spend AFTER bytes are persisted.

This is a grep-based heuristic test — guards against forgetting BOOT-01
when adding new code in Phase 12+. The actual ordering is asserted by
per-module tests (test_ai_talent_frame_renderer.test_render_frame_BOOT_01_ordering
et al.); this test catches the case where a future contributor removes the
calls altogether.

_ltx_api.py is the exception — it's a thin client; caller (assemble.py)
wraps preflight/record around it. Verified separately (estimate_cost helper
must remain present so caller can compute est_cost_usd).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AI_TALENT = REPO / "src" / "ai_talent"

EXTERNAL_API_MODULES: dict[str, dict[str, str]] = {
    "script_builder.py": {
        "provider": "anthropic",
        "api_call_pattern": r"messages\.create\(",
    },
    "frame_renderer.py": {
        "provider": "replicate",
        "api_call_pattern": r"\.run\(",
    },
    "voice_synth.py": {
        "provider": "elevenlabs",
        "api_call_pattern": r"text_to_speech\.(convert|convert_with_timestamps)",
    },
    "_ltx_api.py": {
        "provider": "ltx",
        "api_call_pattern": r"requests\.post\(",
    },
}


def _exclude_comments(text: str) -> str:
    r"""Strip # comments and triple-quoted docstrings so they don't count toward grep hits.

    Naive but sufficient: handles a triple-double-quote on its own line.
    Inline single-line docstrings not stripped.
    """
    lines = []
    in_doc = False
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # Toggle on/off; if same line opens and closes, skip the inside
            if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                continue
            in_doc = not in_doc
            continue
        if in_doc:
            continue
        if stripped.startswith("#"):
            continue
        lines.append(ln)
    return "\n".join(lines)


def test_ai_talent_modules_use_BOOT_01():
    """Every external-call module must call preflight_check + record_provider_spend.

    _ltx_api is exempt: it's a thin client wrapper; the caller (assemble.py)
    is responsible for BOOT-01 bracketing. We only assert it exposes
    estimate_cost so callers can compute the est_cost_usd argument.
    """
    missing: list[str] = []
    for module, spec in EXTERNAL_API_MODULES.items():
        path = AI_TALENT / module
        if not path.exists():
            missing.append(f"{module}: file missing")
            continue
        src = _exclude_comments(path.read_text(encoding="utf-8"))
        if module == "_ltx_api.py":
            if "def estimate_cost" not in src:
                missing.append(f"{module}: estimate_cost helper missing")
            if not re.search(spec["api_call_pattern"], src):
                missing.append(
                    f"{module}: external API call pattern "
                    f"{spec['api_call_pattern']!r} not found"
                )
            continue
        has_preflight = "preflight_check(" in src
        has_record = "record_provider_spend(" in src
        has_api_call = re.search(spec["api_call_pattern"], src) is not None
        if not has_preflight:
            missing.append(f"{module}: preflight_check() missing")
        if not has_record:
            missing.append(f"{module}: record_provider_spend() missing")
        if not has_api_call:
            missing.append(
                f"{module}: external API call pattern "
                f"{spec['api_call_pattern']!r} not found"
            )
    assert not missing, "BOOT-01 invariant violations:\n  " + "\n  ".join(missing)


def test_voice_synth_also_uses_BOOT_02():
    """ElevenLabs caller must gate through require_paid_tier (BOOT-02).

    Indirect path: voice_synth imports voice_selector._make_client, which calls
    require_paid_tier() before constructing the client.
    """
    path = AI_TALENT / "voice_synth.py"
    src = _exclude_comments(path.read_text(encoding="utf-8"))
    assert "_make_client" in src or "require_paid_tier" in src, (
        "voice_synth.py must indirectly invoke BOOT-02 "
        "(via voice_selector._make_client)"
    )


def test_script_builder_omits_unit_field_for_anthropic():
    """spend_tracker_v2 requires unit_field for replicate/elevenlabs/ltx
    but NOT for anthropic (PROVIDER_UNIT_FIELDS['anthropic'] = None)."""
    path = AI_TALENT / "script_builder.py"
    src = _exclude_comments(path.read_text(encoding="utf-8"))
    m = re.search(
        r"record_provider_spend\([^)]*?[\"']anthropic[\"'][^)]*?\)",
        src,
        re.DOTALL,
    )
    assert m, "could not find record_provider_spend(..., 'anthropic', ...) call"
    body = m.group(0)
    assert "unit_field" not in body, (
        "anthropic record_provider_spend must NOT pass unit_field "
        "(per spend_tracker_v2 contract)"
    )


def test_frame_renderer_uses_predict_seconds_unit_field():
    path = AI_TALENT / "frame_renderer.py"
    src = _exclude_comments(path.read_text(encoding="utf-8"))
    assert 'unit_field="predict_seconds"' in src or "unit_field='predict_seconds'" in src


def test_voice_synth_uses_characters_unit_field():
    path = AI_TALENT / "voice_synth.py"
    src = _exclude_comments(path.read_text(encoding="utf-8"))
    assert 'unit_field="characters"' in src or "unit_field='characters'" in src


def test_script_builder_uses_forced_tool_choice():
    """T-11-05-01 mitigation: tool_choice must force emit_video_script."""
    path = AI_TALENT / "script_builder.py"
    src = _exclude_comments(path.read_text(encoding="utf-8"))
    assert 'tool_choice' in src
    assert 'emit_video_script' in src
    # The forced shape must appear together
    m = re.search(
        r'tool_choice\s*=\s*\{[^}]*"type"\s*:\s*"tool"[^}]*"name"\s*:\s*"emit_video_script"',
        src,
        re.DOTALL,
    )
    assert m, "script_builder.py must force tool_choice={'type':'tool','name':'emit_video_script'}"
