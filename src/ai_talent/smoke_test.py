"""Phase 9 Plan 04 smoke-test — 5 LOCKED prompts → 5 PNGs → 1×5 collage.

Anchor fixture invariant: the 5 prompts in SMOKE_PROMPTS are LOCKED. Plan 05
(identity_anchor.py) and a future v2 LoRA both re-run these exact prompts to
verify CHAR-06 cosine ≥0.85 against v1. Do NOT edit prompts without bumping
the fixture name (e.g. SMOKE_PROMPTS_V2) and creating a fresh anchor file.

BOOT-01 invariant: every replicate.run() is wrapped by preflight_check (before)
+ record_provider_spend (after). Provider monthly cap forced to 6.0 USD —
matches DEFAULT_PROVIDER_MONTHLY_CAPS["replicate"]+2 buffer for Phase 9 LoRA
training overhang. Total smoke cost ≈ 5 × $0.025 = $0.13.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final

import replicate
import yaml
from PIL import Image

from src.spend_tracker_v2 import preflight_check, record_provider_spend


# --- LOCKED anchor prompts -------------------------------------------------
# WARNING: these are the anchor fixture for CHAR-05 (operator ≥70%) AND
# CHAR-06 (Facenet512 cosine ≥0.85 v1 vs v2). DO NOT modify without bumping
# the fixture name + persisting the new variant to a fresh anchor file.
SMOKE_PROMPTS: Final[tuple[tuple[str, str], ...]] = (
    (
        "01_closeup",
        "OHWX_FORTONA, close-up portrait, neutral expression, soft golden hour "
        "light, looking at camera, lifestyle photograph",
    ),
    (
        "02_three_quarter",
        "OHWX_FORTONA, 3/4 body shot, 3/4 view, warm smile, diffused daylight, "
        "coffee shop background",
    ),
    (
        "03_fullbody",
        "OHWX_FORTONA, full body shot, standing confidently in autumn street, "
        "cream blouse, lifestyle cinema",
    ),
    (
        "04_profile",
        "OHWX_FORTONA, side profile, contemplative expression, cinematic warm "
        "tungsten, dark neutral background",
    ),
    (
        "05_emotion",
        "OHWX_FORTONA, close-up portrait, laughing genuinely, eyes crinkled, "
        "soft daylight, lifestyle",
    ),
)

COST_PER_INFERENCE_USD: Final[float] = 0.025
PREDICT_SECONDS_PER_RUN: Final[int] = 6
PROVIDER_MONTHLY_CAP_USD: Final[float] = 6.0  # Phase 9 budget for LoRA + smoke

# Resolve repo root from this file location → marketing-v3/.metrics/* etc.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_YAML_PATH: Final[Path] = _REPO_ROOT / "ai_talent" / "character.yaml"
DEFAULT_SMOKE_DIR: Final[Path] = _REPO_ROOT / "ai_talent" / "smoke" / "v1"

# 1×5 collage tuning
COLLAGE_GAP_PX: Final[int] = 8
COLLAGE_BG_RGB: Final[tuple[int, int, int]] = (26, 15, 8)  # brand #1A0F08


class SmokeTestError(RuntimeError):
    """Raised on misconfig or LoRA-not-ready state."""


def _make_client() -> replicate.Client:
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        raise SmokeTestError("REPLICATE_API_TOKEN env var is missing")
    return replicate.Client(api_token=token)


def write_anchor_prompts(out_dir: Path) -> Path:
    """Persist the 5 LOCKED prompts as tab-delimited fixture.

    Each line: ``<name>\\t<prompt>``. Plan 05 reads this file verbatim.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "anchor_prompts.txt"
    lines = [f"{name}\t{prompt}" for name, prompt in SMOKE_PROMPTS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_collage(
    image_paths: list[Path],
    out_path: Path,
    *,
    thumb_h: int = 768,
) -> Path:
    """1×5 horizontal collage. Thumbnails scaled to thumb_h preserving aspect.

    Padding: COLLAGE_GAP_PX (8px) between thumbs, no outer border. Background
    = brand #1A0F08 (Forton dark gold-on-bronze).
    """
    if len(image_paths) != 5:
        raise SmokeTestError(
            f"build_collage requires exactly 5 images; got {len(image_paths)}"
        )
    thumbs: list[Image.Image] = []
    for p in image_paths:
        im = Image.open(p).convert("RGB")
        ratio = thumb_h / im.height
        new_w = int(im.width * ratio)
        thumbs.append(im.resize((new_w, thumb_h), Image.LANCZOS))

    total_w = sum(t.width for t in thumbs) + 4 * COLLAGE_GAP_PX
    canvas = Image.new("RGB", (total_w, thumb_h), COLLAGE_BG_RGB)
    x = 0
    for t in thumbs:
        canvas.paste(t, (x, 0))
        x += t.width + COLLAGE_GAP_PX

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG")
    return out_path


def run_smoke(
    yaml_path: Path = DEFAULT_YAML_PATH,
    *,
    out_dir: Path = DEFAULT_SMOKE_DIR,
    spend_file: Path = DEFAULT_SPEND_FILE,
    client: replicate.Client | None = None,
) -> list[Path]:
    """Run 5 LOCKED prompts against the trained LoRA → 5 PNGs + 1 collage.

    Flow per frame (BOOT-01 invariant — asserted by tests):
      1. preflight_check(provider="replicate", est_cost_usd=0.025, cap=6.0)
      2. client.run(full_ref, input={prompt, 9:16, png, 28 steps, guidance 3.5})
      3. write bytes to out_dir/<name>.png
      4. record_provider_spend(usd=0.025, units=6, field="predict_seconds")

    After all 5 frames:
      - write anchor_prompts.txt (tab-delimited fixture for Plan 05 + v2)
      - build collage_1x5.png

    Pre-flight refuses if character.yaml.lora.status != "ready".
    """
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    lora = (data or {}).get("lora", {}) or {}
    if lora.get("status") != "ready":
        raise SmokeTestError(
            f"character.yaml.lora.status must be 'ready' before smoke; "
            f"got {lora.get('status')!r} — re-run Plan 03"
        )
    model = lora.get("model")
    version = lora.get("version_sha256")
    if not model or not version:
        raise SmokeTestError(
            f"lora.model and lora.version_sha256 must be set; got "
            f"model={model!r} version={version!r}"
        )
    full_ref = f"{model}:{version}"

    if client is None:
        client = _make_client()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_paths: list[Path] = []
    for name, prompt in SMOKE_PROMPTS:
        # STEP 1: preflight (mandatory — BOOT-01)
        preflight_check(
            spend_file,
            "replicate",
            COST_PER_INFERENCE_USD,
            provider_monthly_cap=PROVIDER_MONTHLY_CAP_USD,
        )

        # STEP 2: Replicate API call
        output = client.run(
            full_ref,
            input={
                "prompt": prompt,
                "aspect_ratio": "9:16",
                "output_format": "png",
                "num_outputs": 1,
                "guidance_scale": 3.5,
                "num_inference_steps": 28,
            },
        )
        img_bytes = output[0].read()

        # STEP 3: persist
        target = out_dir / f"{name}.png"
        target.write_bytes(img_bytes)
        out_paths.append(target)

        # STEP 4: record spend (mandatory — BOOT-01)
        record_provider_spend(
            spend_file,
            "replicate",
            usd=COST_PER_INFERENCE_USD,
            units=PREDICT_SECONDS_PER_RUN,
            unit_field="predict_seconds",
        )

    # Side artifacts (operator + Plan 05)
    write_anchor_prompts(out_dir)
    build_collage(out_paths, out_dir / "collage_1x5.png")
    return out_paths


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 9 Plan 04: smoke-test trained LoRA against 5 LOCKED prompts."
    )
    ap.add_argument(
        "--yaml-path",
        default=str(DEFAULT_YAML_PATH),
        help="Path to character.yaml (default: ai_talent/character.yaml)",
    )
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_SMOKE_DIR),
        help="Output directory (default: ai_talent/smoke/v1/)",
    )
    args = ap.parse_args(argv)

    out_paths = run_smoke(Path(args.yaml_path), out_dir=Path(args.out_dir))
    for p in out_paths:
        print(p)
    print(f"collage: {Path(args.out_dir) / 'collage_1x5.png'}")
    print(f"anchor:  {Path(args.out_dir) / 'anchor_prompts.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
