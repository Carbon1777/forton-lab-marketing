"""Phase 9 Plan 01 — dataset generator (Flux dev base, 30 variation-matrix frames).

Output: ai_talent/dataset/v1/{NN}.jpg + {NN}.txt pairs + MANIFEST.json.

BOOT-01 invariant — every replicate.run() call is wrapped in:
    preflight_check(...) → client.run(...) → write_bytes(...) → record_provider_spend(...)
with provider_monthly_cap=6.0 override (default $4 not enough for Phase 8 + Phase 9 cumulative).

Caption strategy (09-RESEARCH §Caption Strategy + §Pitfall 2):
    caption = "OHWX_FORTONA, {frame_type}, {angle}, {expression}"
Identifying features (brunette / blue eyes / age / "Russian" / "woman") are DELIBERATELY
omitted — captions describe composition only so the LoRA learns "who" (the trigger word)
without contaminating with redundant descriptors.

Single source of truth for character card text: marketing-v3/ai_talent/character.yaml
→ phase_8.character_card. Trigger word is locked as TRIGGER_WORD_LOCKED below and must
mirror character.yaml.lora.trigger_word.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any, Final

import replicate
import yaml

from src.spend_tracker_v2 import (
    DEFAULT_PROVIDER_MONTHLY_CAPS,  # noqa: F401  (referenced by docstring)
    preflight_check,
    record_provider_spend,
)

# --- Module constants -------------------------------------------------------
MODEL_REF: Final[str] = "black-forest-labs/flux-dev"
TRIGGER_WORD_LOCKED: Final[str] = "OHWX_FORTONA"  # mirrors character.yaml.lora.trigger_word
COST_PER_FRAME_USD: Final[float] = 0.025  # Replicate Flux dev verified 2026-05-10
PREDICT_SECONDS_PER_FRAME: Final[int] = 8  # rough estimate per Flux dev call (no LoRA)
PROVIDER_MONTHLY_CAP_USD: Final[float] = 6.0  # Phase 9 override (default $4 too low)

# Resolve relative to module file (Phase 9-prep hot-fix #29 pattern).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_DATASET_DIR: Final[Path] = _REPO_ROOT / "ai_talent" / "dataset" / "v1"
DEFAULT_CHARACTER_YAML: Final[Path] = _REPO_ROOT / "ai_talent" / "character.yaml"


# --- Variation matrix -------------------------------------------------------
# 30 balanced combos: ≥6 per frame_type bucket (09-RESEARCH §Code Examples + Pitfall 1).
COMPOSITIONS_30: Final[list[tuple[str, str, str]]] = [
    # 6 close-up
    ("close-up portrait", "front-facing", "neutral expression"),
    ("close-up portrait", "3/4 view", "soft smile"),
    ("close-up portrait", "front", "warm laughing"),
    ("close-up portrait", "slight tilt", "contemplative"),
    ("close-up portrait", "3/4 view", "serious"),
    ("close-up portrait", "front", "genuine smile"),
    # 6 medium
    ("medium shot", "front", "neutral"),
    ("medium shot", "3/4 view", "warm smile"),
    ("medium shot", "side profile", "thoughtful"),
    ("medium shot", "front", "speaking gesture"),
    ("medium shot", "3/4 view", "engaged"),
    ("medium shot", "front", "confident"),
    # 6 three-quarter body
    ("3/4 body shot", "3/4 view", "neutral"),
    ("3/4 body shot", "front", "soft smile"),
    ("3/4 body shot", "side profile", "looking away"),
    ("3/4 body shot", "3/4 view", "warm"),
    ("3/4 body shot", "front", "speaking"),
    ("3/4 body shot", "3/4 view", "relaxed"),
    # 6 full body
    ("full body shot", "front, standing", "confident"),
    ("full body shot", "3/4 view, walking", "casual"),
    ("full body shot", "side profile, walking", "natural"),
    ("full body shot", "front, sitting", "relaxed"),
    ("full body shot", "3/4 view, standing", "warm"),
    ("full body shot", "front, leaning", "casual"),
    # 6 lifestyle
    ("lifestyle medium", "natural pose, holding coffee", "warm smile"),
    ("lifestyle 3/4 body", "natural pose, working at laptop", "focused"),
    ("lifestyle close", "candid moment, looking away", "soft"),
    ("lifestyle medium", "natural pose, reading book", "contemplative"),
    ("lifestyle 3/4 body", "natural pose, walking outdoors", "happy"),
    ("lifestyle close", "candid laughing", "joyful"),
]

LIGHTING_3: Final[list[str]] = [
    "soft golden hour",
    "diffused daylight",
    "cinematic warm tungsten",
]
SETTINGS_3: Final[list[str]] = [
    "indoor neutral beige background",
    "cosy coffee shop blurred",
    "outdoor city street autumn",
]
OUTFITS_3: Final[list[str]] = [
    "oversized warm beige knit pullover, simple thin gold chain",
    "casual cream silk blouse, minimal makeup",
    "smart-casual dark sweater, simple jewelry",
]


class DatasetGenerationError(Exception):
    """Raised on env-misconfig or invalid character_card."""


# --- Helpers ----------------------------------------------------------------
def build_caption(frame_type: str, angle: str, expression: str) -> str:
    """Locked caption template — trigger word + composition only.

    NO identifying features (per 09-RESEARCH §Pitfall 2): the LoRA should learn
    "who OHWX_FORTONA is" from images, not from text descriptors that would
    over-anchor surface features (hair color, eye color, age, ethnicity).
    """
    return f"{TRIGGER_WORD_LOCKED}, {frame_type}, {angle}, {expression}"


def _validate_output_path(path: Path) -> None:
    """Reject non-JPG output paths — ostris LoRA trainer expects JPG datasets.

    WebP and PNG are explicit anti-patterns per 09-RESEARCH §Anti-Patterns.
    """
    if path.suffix.lower() != ".jpg":
        raise ValueError(
            f"Only .jpg allowed (ostris trainer safety, no WebP, no PNG); "
            f"got '{path.suffix}' for {path}"
        )


def _build_prompt(
    card: str,
    frame_type: str,
    angle: str,
    expression: str,
    lighting: str,
    outfit: str,
    setting: str,
) -> str:
    return (
        f"{card} Wearing {outfit}. "
        f"{frame_type}, {angle}, {expression}. "
        f"Setting: {setting}. Lighting: {lighting}. "
        "Stylized cinema look, lifestyle photograph, 35mm film aesthetic, "
        "natural skin texture, shallow depth of field."
    )


# --- Core API ---------------------------------------------------------------
def generate_frame(
    client: replicate.Client,
    card: str,
    index: int,
    *,
    out_dir: Path = DEFAULT_DATASET_DIR,
    spend_file: Path = DEFAULT_SPEND_FILE,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Generate one frame at variation-matrix `index` in [1..30].

    Flow (BOOT-01 — order asserted by tests):
      1. preflight_check(provider_monthly_cap=6.0) — may raise cap errors.
      2. client.run(MODEL_REF, input={...})        — Replicate Flux dev.
      3. write image bytes to {out_dir}/{NN}.jpg + write caption to {NN}.txt.
      4. record_provider_spend(replicate, predict_seconds=8).

    If preflight raises, no Replicate call and no spend recorded — exception propagates.

    Returns: per-frame manifest record (filename, prompt, seed, all variation axes, sha256).
    """
    if not 1 <= index <= len(COMPOSITIONS_30):
        raise ValueError(f"index out of range 1..{len(COMPOSITIONS_30)}; got {index}")

    rng = rng or random.Random()
    frame_type, angle, expression = COMPOSITIONS_30[index - 1]
    lighting = LIGHTING_3[index % 3]
    setting = SETTINGS_3[(index // 3) % 3]
    outfit = OUTFITS_3[(index // 5) % 3]
    seed = rng.randint(1, 2**31 - 1)
    prompt = _build_prompt(card, frame_type, angle, expression, lighting, outfit, setting)

    out_path = out_dir / f"{index:02d}.jpg"
    _validate_output_path(out_path)

    # STEP 1: BOOT-01 preflight (mandatory, $6 provider cap override)
    preflight_check(
        spend_file,
        "replicate",
        COST_PER_FRAME_USD,
        provider_monthly_cap=PROVIDER_MONTHLY_CAP_USD,
    )

    # STEP 2: Replicate Flux dev call
    aspect = "9:16" if ("full body" in frame_type or "3/4 body" in frame_type) else "1:1"
    output = client.run(
        MODEL_REF,
        input={
            "prompt": prompt,
            "aspect_ratio": aspect,
            "output_format": "jpg",
            "output_quality": 95,
            "num_inference_steps": 40,
            "guidance": 3.5,
            "seed": seed,
            "num_outputs": 1,
        },
    )
    # SDK 1.0+ returns list[FileOutput]; .read() → bytes
    image_bytes = output[0].read()

    # STEP 3: persist jpg + caption
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image_bytes)
    caption = build_caption(frame_type, angle, expression)
    out_path.with_suffix(".txt").write_text(caption + "\n", encoding="utf-8")

    # STEP 4: BOOT-01 record spend
    record_provider_spend(
        spend_file,
        "replicate",
        usd=COST_PER_FRAME_USD,
        units=PREDICT_SECONDS_PER_FRAME,
        unit_field="predict_seconds",
    )

    return {
        "index": index,
        "filename": out_path.name,
        "prompt": prompt,
        "seed": seed,
        "frame_type": frame_type,
        "angle": angle,
        "expression": expression,
        "lighting": lighting,
        "outfit": outfit,
        "setting": setting,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
    }


def generate_dataset(
    card: str,
    *,
    out_dir: Path = DEFAULT_DATASET_DIR,
    spend_file: Path = DEFAULT_SPEND_FILE,
    client: replicate.Client | None = None,
    seed_rng: int = 42,
) -> list[dict[str, Any]]:
    """Generate all 30 frames; write MANIFEST.json.

    Idempotent — skips frames whose .jpg already exists on disk. Re-runs after a
    manual delete (reroll) regenerate only missing frames.
    """
    if client is None:
        import os
        token = os.environ.get("REPLICATE_API_TOKEN")
        if not token:
            raise DatasetGenerationError("REPLICATE_API_TOKEN env var is missing")
        client = replicate.Client(api_token=token)

    rng = random.Random(seed_rng)
    out_dir.mkdir(parents=True, exist_ok=True)
    new_records: list[dict[str, Any]] = []

    # Load existing manifest entries (preserve audit trail across reroll re-runs).
    manifest_path = out_dir / "MANIFEST.json"
    existing: dict[int, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
            for r in prev.get("frames", []):
                existing[int(r["index"])] = r
        except (json.JSONDecodeError, OSError, KeyError):
            existing = {}

    for i in range(1, len(COMPOSITIONS_30) + 1):
        target = out_dir / f"{i:02d}.jpg"
        if target.exists():
            continue
        # Each generate_frame uses rng.randint → unique seeds per call → reroll
        # after a delete produces a different image.
        new_records.append(
            generate_frame(client, card, i, out_dir=out_dir, spend_file=spend_file, rng=rng)
        )

    # Merge existing + new, keyed by index, write manifest.
    merged: dict[int, dict[str, Any]] = dict(existing)
    for r in new_records:
        merged[int(r["index"])] = r
    frames_sorted = [merged[k] for k in sorted(merged.keys())]
    manifest_path.write_text(
        json.dumps(
            {
                "trigger_word": TRIGGER_WORD_LOCKED,
                "model_ref": MODEL_REF,
                "frames": frames_sorted,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return new_records


# --- CLI --------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 9 Plan 01 — generate 30-frame LoRA dataset via Flux dev"
    )
    ap.add_argument(
        "--character-yaml",
        default=str(DEFAULT_CHARACTER_YAML),
        help="Path to character.yaml (default: %(default)s)",
    )
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_DATASET_DIR),
        help="Dataset output directory (default: %(default)s)",
    )
    ap.add_argument(
        "--seed-rng",
        type=int,
        default=42,
        help="RNG seed for per-frame seed selection (default: %(default)s)",
    )
    args = ap.parse_args(argv)

    yaml_path = Path(args.character_yaml)
    if not yaml_path.exists():
        sys.stderr.write(f"ERROR: character.yaml not found at {yaml_path}\n")
        return 1
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    card = (data.get("phase_8") or {}).get("character_card")
    if not card:
        sys.stderr.write(
            "ERROR: character.yaml.phase_8.character_card is empty — Phase 8 not closed?\n"
        )
        return 1

    records = generate_dataset(card, out_dir=Path(args.out_dir), seed_rng=args.seed_rng)
    print(
        f"OK: generated {len(records)} new frames; "
        f"manifest at {args.out_dir}/MANIFEST.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
