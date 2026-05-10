"""Phase 8 character design: 3 variants × 4 frames via Replicate Flux dev.

Every API call is gated through spend_tracker_v2 (BOOT-01 invariant):
    preflight_check("replicate", est_cost_usd) → replicate.run(...) → record_provider_spend(...).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

import replicate

from src.spend_tracker_v2 import preflight_check, record_provider_spend

# --- Module constants ---
MODEL_REF: Final[str] = "black-forest-labs/flux-dev"
COST_PER_FRAME_USD: Final[float] = 0.025  # Replicate verified 2026-05-10
PREDICT_SECONDS_PER_FRAME: Final[int] = 14  # rough estimate, refined after first probe

LIGHTING: Final[str] = (
    "Warm cinematic lighting, golden-hour soft light, shallow depth of field, "
    "photorealistic, editorial portrait quality, 35mm film aesthetic, "
    "natural skin texture, brand-palette accents (deep brown #1A0F08 background, warm gold highlights)"
)

COMPOSITIONS: Final[dict[str, str]] = {
    "closeup":   "Tight portrait, face fills 60% of frame, shoulders visible, shallow depth of field f/1.8, eye-level angle",
    "medium":    "Medium shot from waist up, hands relaxed visible, standing or seated upright, natural pose",
    "fullbody":  "Full body standing pose, slight contrapposto, casual confident stance, environment context visible",
    "lifestyle": "Lifestyle candid moment capturing authentic action, environment-driven framing",
}

DEFAULT_SPEND_FILE: Final[Path] = Path("marketing-v3/.metrics/api_spend.json")
DEFAULT_CACHE_DIR: Final[Path] = Path("marketing-v3/.cache/character_preview/v1")


class CharacterDesignError(Exception):
    """Raised on env-misconfig or Replicate-side errors."""


def _make_client() -> replicate.Client:
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        raise CharacterDesignError("REPLICATE_API_TOKEN env var is missing")
    return replicate.Client(api_token=token)


def _variant_seed(card: str) -> int:
    """Deterministic seed per variant — first 8 hex chars of sha256, masked to int32."""
    h = hashlib.sha256(card.encode("utf-8")).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


def generate_frame(
    client: replicate.Client,
    card: str,
    composition_name: str,
    out_path: Path,
    *,
    spend_file: Path = DEFAULT_SPEND_FILE,
    est_cost_usd: float = COST_PER_FRAME_USD,
) -> Path:
    """Single Replicate Flux dev call with mandatory spend gate (BOOT-01).

    Flow (order is asserted by tests):
      1. preflight_check("replicate", est_cost_usd)  — may raise cap errors.
      2. client.run(MODEL_REF, input={...})          — Replicate API.
      3. write bytes to disk at `out_path`.
      4. record_provider_spend("replicate", ..., unit_field="predict_seconds").

    If preflight raises, no Replicate call, no spend recording — propagates.
    """
    composition = COMPOSITIONS[composition_name]
    prompt = f"{card}\n{composition}\n{LIGHTING}"
    seed = _variant_seed(card)

    # STEP 1: preflight (mandatory — BOOT-01)
    preflight_check(spend_file, "replicate", est_cost_usd)

    # STEP 2: Replicate API call
    output = client.run(
        MODEL_REF,
        input={
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "output_format": "png",
            "output_quality": 100,
            "num_inference_steps": 40,
            "guidance": 3.5,
            "seed": seed,
            "num_outputs": 1,
        },
    )
    # SDK 1.0+ returns list[FileOutput]; .read() → bytes
    image_bytes = output[0].read()

    # STEP 3: persist
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image_bytes)

    # STEP 4: record spend (mandatory — BOOT-01)
    record_provider_spend(
        spend_file,
        "replicate",
        usd=est_cost_usd,
        units=PREDICT_SECONDS_PER_FRAME,
        unit_field="predict_seconds",
    )
    return out_path


def generate_batch(
    cards: dict[str, str],
    out_dir: Path = DEFAULT_CACHE_DIR,
    *,
    spend_file: Path = DEFAULT_SPEND_FILE,
    client: replicate.Client | None = None,
) -> dict[str, list[Path]]:
    """Generate all 12 frames (3 variants × 4 compositions).

    Args:
        cards: {"variant_1": "<character card text>", "variant_2": ..., "variant_3": ...}
        out_dir: target directory; per-variant subdirs created automatically.
        spend_file: spend tracker JSON path.
        client: optional pre-built Replicate client (else _make_client()).

    Returns:
        {"variant_1": [closeup_path, medium_path, fullbody_path, lifestyle_path], ...}
    """
    if client is None:
        client = _make_client()
    result: dict[str, list[Path]] = {}
    for variant_id, card in cards.items():
        variant_paths: list[Path] = []
        for comp_name in ("closeup", "medium", "fullbody", "lifestyle"):
            out_path = out_dir / variant_id / f"{comp_name}.png"
            generate_frame(client, card, comp_name, out_path, spend_file=spend_file)
            variant_paths.append(out_path)
        result[variant_id] = variant_paths
    return result
