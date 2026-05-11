"""Stage 2 — Replicate Flux+LoRA frame renderer.

Differs from Phase 8 ``character_designer.generate_frame``:
    * Uses TRAINED LoRA full_ref (``model:version_sha256``) instead of base
      ``black-forest-labs/flux-dev``.
    * Asserts ``character.yaml.lora.status == 'ready'`` at call time
      (T-11-05-03 stale-LoRA mitigation — version_sha256 pinned).
    * Validates prompt starts with ``OHWX_FORTONA`` (script_builder normally
      guarantees this, but defense in depth — direct callers may bypass).

BOOT-01 4-step (asserted by ``test_ai_talent_BOOT_01_invariant``):
    preflight_check -> replicate.run -> write bytes -> record_provider_spend.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import replicate
import yaml

from src.spend_tracker_v2 import preflight_check, record_provider_spend

COST_PER_FRAME_USD: Final[float] = 0.025
PREDICT_SECONDS_PER_FRAME: Final[int] = 14
TRIGGER_WORD: Final[str] = "OHWX_FORTONA"

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_CHARACTER_YAML: Final[Path] = _REPO_ROOT / "ai_talent" / "character.yaml"


class FrameRendererError(RuntimeError):
    """Raised on missing env, lora-not-ready, missing trigger, or Replicate errors."""


def resolve_lora_ref(char_yaml_path: Path = DEFAULT_CHARACTER_YAML) -> str:
    """Read character.yaml -> return ``model:version_sha256`` full reference.

    Asserts lora.status == 'ready' (T-11-05-03 mitigation).
    """
    if not char_yaml_path.exists():
        raise FrameRendererError(f"character.yaml missing: {char_yaml_path}")
    data = yaml.safe_load(char_yaml_path.read_text(encoding="utf-8"))
    lora = (data or {}).get("lora", {}) or {}
    if lora.get("status") != "ready":
        raise FrameRendererError(
            f"character.yaml.lora.status must be 'ready'; got {lora.get('status')!r}"
        )
    model = lora.get("model")
    version = lora.get("version_sha256")
    if not model or not version:
        raise FrameRendererError(
            "lora.model + lora.version_sha256 required in character.yaml"
        )
    return f"{model}:{version}"


def _make_client() -> replicate.Client:
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        raise FrameRendererError("REPLICATE_API_TOKEN env var missing")
    return replicate.Client(api_token=token)


def render_frame(
    prompt: str,
    out_path: Path,
    *,
    client: Any | None = None,
    char_yaml_path: Path = DEFAULT_CHARACTER_YAML,
    spend_file: Path = DEFAULT_SPEND_FILE,
    est_cost_usd: float = COST_PER_FRAME_USD,
    seed: int | None = None,
) -> Path:
    """Single Replicate Flux+LoRA frame render with mandatory BOOT-01 spend gate.

    Flow:
      1. preflight_check("replicate", est_cost_usd)
      2. client.run(model:version_sha256, input={prompt, 9:16, png})
      3. write png bytes to ``out_path``
      4. record_provider_spend("replicate", units=PREDICT_SECONDS_PER_FRAME,
                                unit_field="predict_seconds")

    Validates ``prompt`` starts with OHWX_FORTONA (defense layer 3 — script_builder
    is L2, SYSTEM prompt is L1).
    """
    if not prompt.startswith(TRIGGER_WORD):
        raise FrameRendererError(
            f"prompt must start with {TRIGGER_WORD}; got: {prompt[:64]!r}"
        )
    full_ref = resolve_lora_ref(char_yaml_path)
    if client is None:
        client = _make_client()

    # STEP 1: preflight (BOOT-01)
    preflight_check(spend_file, "replicate", est_cost_usd)

    # STEP 2: Replicate API call with LoRA full_ref
    api_input: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": "9:16",
        "output_format": "png",
        "output_quality": 92,
        "num_inference_steps": 28,
        "guidance_scale": 3.5,
        "num_outputs": 1,
    }
    if seed is not None:
        api_input["seed"] = seed
    output = client.run(full_ref, input=api_input)
    # SDK 1.0+ returns list[FileOutput] OR a single FileOutput
    if hasattr(output, "read"):
        img_bytes = output.read()
    else:
        img_bytes = output[0].read()

    # STEP 3: persist
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(img_bytes)

    # STEP 4: record spend (BOOT-01)
    record_provider_spend(
        spend_file, "replicate",
        usd=est_cost_usd,
        units=PREDICT_SECONDS_PER_FRAME,
        unit_field="predict_seconds",
    )
    return out_path


__all__ = [
    "FrameRendererError",
    "render_frame",
    "resolve_lora_ref",
    "COST_PER_FRAME_USD",
    "PREDICT_SECONDS_PER_FRAME",
    "TRIGGER_WORD",
]
