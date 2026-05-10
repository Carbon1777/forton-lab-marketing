"""Phase 9 LoRA trainer — ostris/flux-dev-lora-trainer wrapper.

Sync foreground polling (every 60s, hard timeout 45 min). BOOT-01 spend gate.
Output schema is RESEARCH Q1 unverified — full payload logged to .cache/lora_v1/.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Final

import replicate

from src.spend_tracker_v2 import preflight_check, record_provider_spend

TRAINER_REF: Final[str] = "ostris/flux-dev-lora-trainer"
DEST_MODEL_NAME: Final[str] = "forton-lab-character-v1"
TRIGGER_WORD_LOCKED: Final[str] = "OHWX_FORTONA"
DEFAULT_STEPS: Final[int] = 1000
STEPS_CAP: Final[int] = 1500
LORA_RANK: Final[int] = 16
LEARNING_RATE: Final[float] = 0.0004
OPTIMIZER: Final[str] = "adamw8bit"
AUTOCAPTION: Final[bool] = False
CAPTION_DROPOUT_RATE: Final[float] = 0.05
RESOLUTION: Final[str] = "512,768,1024"

POLL_INTERVAL_SEC: Final[int] = 60
TRAINING_TIMEOUT_SEC: Final[int] = 45 * 60
EST_COST_USD: Final[float] = 2.20
PREDICT_SECONDS_BUDGET: Final[int] = 1800
PROVIDER_MONTHLY_CAP_USD: Final[float] = 6.0

# Resolve paths via __file__ (per PR #31 hot-fix #29).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_DATASET_DIR: Final[Path] = _REPO_ROOT / "ai_talent" / "dataset" / "v1"
DEFAULT_CACHE_DIR: Final[Path] = _REPO_ROOT / ".cache" / "lora_v1"
DEFAULT_ZIP_PATH: Final[Path] = _REPO_ROOT / "ai_talent" / "dataset" / "v1.zip"


def zip_dataset(src_dir: Path = DEFAULT_DATASET_DIR, out_zip: Path = DEFAULT_ZIP_PATH) -> Path:
    """Zip *.jpg + *.txt pairs (no MANIFEST.json, no nested dirs)."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in ("*.jpg", "*.txt"):
            for p in sorted(src_dir.glob(ext)):
                zf.write(p, arcname=p.name)
    return out_zip


def build_training_input(input_images_url: str, *, steps: int = DEFAULT_STEPS) -> dict[str, Any]:
    """Locked params for ostris/flux-dev-lora-trainer (CHAR-04 invariants)."""
    if steps > STEPS_CAP:
        raise ValueError(f"steps={steps} exceeds CHAR-04 cap STEPS_CAP={STEPS_CAP}")
    return {
        "input_images": input_images_url,
        "trigger_word": TRIGGER_WORD_LOCKED,
        "steps": steps,
        "lora_rank": LORA_RANK,
        "learning_rate": LEARNING_RATE,
        "batch_size": 1,
        "resolution": RESOLUTION,
        "autocaption": AUTOCAPTION,
        "caption_dropout_rate": CAPTION_DROPOUT_RATE,
        "optimizer": OPTIMIZER,
    }


def ensure_destination_model(client: replicate.Client, owner: str) -> str:
    """Create destination model if missing; return full ref `<owner>/<name>`."""
    full = f"{owner}/{DEST_MODEL_NAME}"
    try:
        client.models.create(
            owner=owner,
            name=DEST_MODEL_NAME,
            visibility="private",
            hardware="gpu-h100",
        )
    except Exception as e:
        if "already" not in str(e).lower():
            try:
                client.models.get(full)
            except Exception:
                raise
    return full


def poll_training(
    training_id: str,
    *,
    timeout_sec: int = TRAINING_TIMEOUT_SEC,
    poll_interval: int = POLL_INTERVAL_SEC,
    client: replicate.Client | None = None,
):
    """Poll training until terminal state or timeout (cancel on timeout)."""
    client = client or replicate.Client()
    start = time.monotonic()
    while True:
        tr = client.trainings.get(training_id)
        if tr.status in ("succeeded", "failed", "canceled"):
            return tr
        if time.monotonic() - start > timeout_sec:
            try:
                client.trainings.cancel(training_id)
            except Exception:
                pass
            raise TimeoutError(f"training {training_id} exceeded {timeout_sec}s — canceled")
        time.sleep(poll_interval)


_VERSION_URL_RE: Final = re.compile(r"replicate\.com/([^/]+)/([^/]+)/versions/([0-9a-f]+)")


def extract_result_version(training) -> str:
    """Best-effort: return `<owner>/<model>:<sha>` from training.output or URL fallback."""
    out = training.output or {}
    if isinstance(out, dict):
        if out.get("version"):
            return str(out["version"])
        if out.get("model"):
            return str(out["model"])
    urls = getattr(training, "urls", None) or {}
    get_url = urls.get("get") if isinstance(urls, dict) else None
    if get_url:
        m = _VERSION_URL_RE.search(get_url)
        if m:
            return f"{m.group(1)}/{m.group(2)}:{m.group(3)}"
    raise RuntimeError(f"cannot extract version from training.output={out!r}")


def train_v1(
    owner: str,
    input_images_url: str,
    *,
    steps: int = DEFAULT_STEPS,
    spend_file: Path = DEFAULT_SPEND_FILE,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    client: replicate.Client | None = None,
) -> dict[str, Any]:
    """Full pipeline: trainings.create → poll → extract → record spend.

    BOOT-01 ordering: preflight_check BEFORE create, record_provider_spend AFTER succeeded.
    """
    client = client or replicate.Client()
    cache_dir.mkdir(parents=True, exist_ok=True)

    # STEP 1: BOOT-01 preflight
    preflight_check(
        spend_file,
        "replicate",
        EST_COST_USD,
        provider_monthly_cap=PROVIDER_MONTHLY_CAP_USD,
    )

    # STEP 2: ensure destination + get trainer version SHA
    destination = ensure_destination_model(client, owner)
    trainer_versions = list(client.models.get(TRAINER_REF).versions.list())
    trainer_version_id = trainer_versions[0].id
    trainer_full_ref = f"{TRAINER_REF}:{trainer_version_id}"

    # STEP 3: trainings.create
    training_input = build_training_input(input_images_url, steps=steps)
    training = client.trainings.create(
        version=trainer_full_ref,
        destination=destination,
        input=training_input,
    )
    (cache_dir / "training_id.txt").write_text(training.id, encoding="utf-8")

    # STEP 4: poll until terminal
    training = poll_training(training.id, client=client)

    # STEP 5: dump full output (RESEARCH Q1 audit)
    try:
        (cache_dir / "training_output.json").write_text(
            json.dumps(
                {
                    "id": training.id,
                    "status": training.status,
                    "output": training.output,
                    "error": getattr(training, "error", None),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    if training.status != "succeeded":
        raise RuntimeError(
            f"training failed status={training.status} error={training.error!r}"
        )

    result_version = extract_result_version(training)

    # STEP 6: BOOT-01 record spend
    record_provider_spend(
        spend_file,
        "replicate",
        usd=EST_COST_USD,
        units=PREDICT_SECONDS_BUDGET,
        unit_field="predict_seconds",
    )

    return {
        "training_id": training.id,
        "result_version": result_version,
        "trainer_version": trainer_version_id,
        "destination": destination,
        "steps": steps,
        "rank": LORA_RANK,
        "trigger_word": TRIGGER_WORD_LOCKED,
        "actual_cost_usd": EST_COST_USD,
        "dataset_size": 30,
    }


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase 9 LoRA trainer (live)")
    ap.add_argument("--owner", required=True, help="Replicate account owner (e.g. carbon1777)")
    ap.add_argument(
        "--input-images-url",
        required=False,
        help="Public URL OR replicate file URL of dataset zip (required unless --zip-only)",
    )
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    ap.add_argument("--zip-only", action="store_true", help="Only zip the dataset; do not train")
    args = ap.parse_args(argv)

    if args.zip_only:
        zip_path = zip_dataset()
        print(f"OK: dataset zipped to {zip_path}")
        return 0

    if not args.input_images_url:
        ap.error("--input-images-url is required unless --zip-only")

    result = train_v1(owner=args.owner, input_images_url=args.input_images_url, steps=args.steps)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
