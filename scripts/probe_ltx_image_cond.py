"""Probe: does LTX API accept image_base64 for image-to-video conditioning?

Resolves Open Question Q-LTX-IMG (see .planning/phases/11-pipeline-scaffolding/
11-RESEARCH.md §Open Questions) BEFORE Plan 03 writes
`src/ai_talent/_ltx_api.py`. If image conditioning works, _ltx_api.generate
gets an `image_path` parameter and scenario B uses the chosen hero frame for
identity lock. If it doesn't (HTTP 400 or output bytes identical to text-only),
scenario B falls back to text-only with prompt-only identity hints.

Usage:
    LTX_API_KEY=ltxv_... python scripts/probe_ltx_image_cond.py
    python scripts/probe_ltx_image_cond.py --dry-run    # CI-safe, no API call

Cost: ~$0.80 (2× 5-sec calls @ $0.08/sec × 1080×1920 resolution = $0.40 each).

Result must be appended to /Users/jcat/Documents/Brain/projects/forton-lab/
decisions.md — the script prints the exact entry to copy at the end.

Probe spend is NOT recorded via spend_tracker_v2 (this script uses raw
`requests.post`); operator reconciles via the one-time backfill recipe in
Task 4 of Plan 11-01.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
# Phase 10 reference frame: variant_2/lifestyle.png is the hero candidate
# the LoRA character is portrayed in. Same lighting + portrait orientation
# we want LTX hero shots to match.
HERO_IMG = REPO / "ai_talent" / "preview" / "v1" / "variant_2" / "lifestyle.png"
API_URL = "https://api.ltx.video/v1/text-to-video"
PROBE_PROMPT = (
    "OHWX_FORTONA close-up gentle smile, soft cinematic light, "
    "9:16 portrait orientation, subtle head turn"
)


def _call(prompt: str, image_b64: str | None) -> bytes:
    """Single LTX text-to-video call; returns raw MP4 bytes."""
    key = os.environ.get("LTX_API_KEY")
    if not key:
        raise RuntimeError(
            "LTX_API_KEY not set — see Plan 11-01 Task 3 setup instructions"
        )
    body: dict[str, object] = {
        "prompt": prompt,
        "model": "ltx-2-3-pro",
        "duration": 5,
        "resolution": "1080x1920",
        "fps": 24,
        "generate_audio": False,
    }
    if image_b64 is not None:
        body["image_base64"] = image_b64
    masked = f"{key[:5]}...{key[-2:]}"
    print(f"  POST {API_URL}  (key={masked}, image_b64={'YES' if image_b64 else 'no'})")
    r = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=600,
    )
    r.raise_for_status()
    return r.content


def _emit_brain_entry(answer: str) -> None:
    print()
    print("=" * 60)
    print("Append to /Users/jcat/Documents/Brain/projects/forton-lab/decisions.md:")
    print("=" * 60)
    print(f"## 2026-05-11 — Q-LTX-IMG resolved (Phase 11 Wave 0)")
    print(f"Probe: scripts/probe_ltx_image_cond.py (marketing-v3 sub-repo).")
    print(f"Answer: {answer}.")
    impact = "INCLUDES" if "ACCEPTED" in answer else "OMITS"
    print(f"Impact on Plan 03: _ltx_api.generate {impact} `image_path` parameter.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip LTX API calls (CI-safe, no LTX_API_KEY needed).")
    ap.add_argument("--out-dir", default=str(REPO / ".cache" / "probe_ltx"),
                    help="Where to save the two .mp4 outputs for visual comparison.")
    args = ap.parse_args(argv)

    if args.dry_run:
        print("[dry-run] would call LTX API twice (text-only + text+image_base64)")
        print("[dry-run] expected cost: ~$0.80")
        print(f"[dry-run] reference image: {HERO_IMG}")
        return 0

    if not HERO_IMG.exists():
        print(f"ERROR: reference image missing: {HERO_IMG}", file=sys.stderr)
        return 1
    img_b64 = base64.b64encode(HERO_IMG.read_bytes()).decode()
    print(f"Reference image: {HERO_IMG} ({HERO_IMG.stat().st_size} bytes)")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Call 1/2: text-only...")
    v1 = _call(PROBE_PROMPT, None)
    (out_dir / "text_only.mp4").write_bytes(v1)
    print(f"  saved text_only.mp4 ({len(v1)} bytes)")

    print("Call 2/2: text + image_base64...")
    try:
        v2 = _call(PROBE_PROMPT, img_b64)
        (out_dir / "with_image.mp4").write_bytes(v2)
        print(f"  saved with_image.mp4 ({len(v2)} bytes)")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        body = e.response.text[:300] if e.response is not None else ""
        if status in (400, 422):
            answer = f"image_base64 NOT supported (HTTP {status}: {body[:100]})"
            print(f"=== Q-LTX-IMG ANSWER: {answer} ===")
            print("=== Fallback: text-only LTX, identity via prompt-only ===")
            _emit_brain_entry(answer)
            return 0
        raise

    sha1 = hashlib.sha256(v1).hexdigest()[:12]
    sha2 = hashlib.sha256(v2).hexdigest()[:12]
    print(f"text_only sha={sha1}  with_image sha={sha2}")
    if sha1 == sha2:
        answer = "image_base64 IGNORED by API (identical output)"
    else:
        answer = "image_base64 ACCEPTED and influences output"
    print(f"=== Q-LTX-IMG ANSWER: {answer} ===")
    _emit_brain_entry(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
