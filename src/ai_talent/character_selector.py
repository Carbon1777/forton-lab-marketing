"""Phase 8 character.yaml writer — schema_version=1 manifest mutation.

Anti-replay: write_selection recomputes batch_sha8 from disk before mutation;
if passed sha8 mismatches → SelectionMismatchError, manifest unchanged.

Schema is additive — Phase 9 (lora) and Phase 10 (voice) append without rename.
`trigger_word: OHWX_FORTONA` is locked from Phase 8 onwards.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from src.ai_talent.preview_sender import compute_batch_sha8

SCHEMA_VERSION = 1
CHARACTER_ID = "forton-lab-mascot-v1"
TRIGGER_WORD_LOCKED = "OHWX_FORTONA"  # NEVER rename across phases
VALID_VARIANTS = ("variant_1", "variant_2", "variant_3")
EXPECTED_FRAME_COUNT = 12  # 3 variants × 4 frames

DEFAULT_MANIFEST_PATH = Path("ai_talent/character.yaml")
DEFAULT_FRAME_ROOT = Path(".cache/character_preview/v1")


class SelectionMismatchError(RuntimeError):
    """Raised when passed batch_sha8 does not match recomputed sha8 of frames on disk."""


class LoraTriggerMismatchError(RuntimeError):
    """trigger_word in incoming write != TRIGGER_WORD_LOCKED.

    Defense against accidental rename: the trigger word is baked into the
    trained LoRA weights and into every downstream prompt — silently changing
    it would invalidate the model. Raised BEFORE any mutation, so manifest on
    disk is left untouched.
    """


# SHA detector: accepts 12–64 hex chars at end of string (or bare).
_SHA_RE = re.compile(r"([0-9a-f]{12,64})$")


def _normalize_version_sha(value: str) -> str:
    """Accept either bare SHA or full ref `owner/name:sha`; return SHA only.

    Replicate sometimes returns the resolved model identifier as
    ``owner/name:<64-char-sha>``; callers may also pass just the SHA. We strip
    the prefix (if any) and validate that the remainder is a hex SHA.
    """
    if ":" in value:
        value = value.rsplit(":", 1)[1]
    m = _SHA_RE.search(value)
    if not m or m.group(1) != value:
        raise ValueError(f"version_sha256 not a valid SHA: {value!r}")
    return m.group(1)


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Atomic YAML write: tempfile in same dir → fsync-replace.

    Mirrors `spend_tracker_v2.record_provider_spend` to keep the cross-module
    write pattern uniform (single source of truth for atomicity invariants).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_brief() -> dict[str, Any]:
    return {
        "gender": "female",
        "age_range": "25-28",
        "ethnicity": "caucasian-eastern-european",
        "description": (
            "Брюнетка с голубыми глазами, 25-28 лет, lifestyle stylized cinema look "
            "(не photorealistic). Тёплая для Centry, чёткая для Diktum — через "
            "voice_settings split."
        ),
        "reference_video": "https://youtu.be/E5niFTS3Vm0",
        "reference_timestamp": "12:10",
        "brand_palette": ["#1A0F08", "#D4A640", "#F4C757", "#8B6F2D"],
    }


def _default_lora_block() -> dict[str, Any]:
    return {
        "status": "pending",
        "model": None,
        "version_sha256": None,
        "trigger_word": TRIGGER_WORD_LOCKED,
        "training_dataset_size": None,
        "training_run_id": None,
        "training_cost_usd": None,
    }


def _default_voice_block() -> dict[str, Any]:
    return {
        "status": "pending",
        "provider": "elevenlabs",
        "voice_id": None,
        "voice_settings": {"stability": None, "similarity_boost": None, "style": None},
        "language": "ru",
        "sample_url": None,
        "splits": {"centry": None, "diktum": None},
    }


def _default_phase_8_block() -> dict[str, Any]:
    return {
        "status": "pending",
        "variants_generated": 3,
        "frames_per_variant": 4,
        "selected_variant": None,
        "selected_at": None,
        "selected_by": None,
        "batch_sha8": None,
        "preview_dir": "ai_talent/preview/v1/",
        "total_spend_usd": None,
        "character_card": None,
        "regen_count": 0,
    }


def write_initial_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> None:
    """Create the v0 placeholder. Idempotent — no-op if file already exists."""
    if path.exists():
        return
    today = dt.date.today().isoformat()
    data = {
        "schema_version": SCHEMA_VERSION,
        "character_id": CHARACTER_ID,
        "created_at": today,
        "updated_at": None,
        "brief": _default_brief(),
        "phase_8": _default_phase_8_block(),
        "lora": _default_lora_block(),
        "voice": _default_voice_block(),
        "history": [
            {
                "phase": 8,
                "event": "created",
                "at": today,
                "note": "Initial structure scaffolded",
            }
        ],
    }
    _atomic_write_yaml(path, data)


def read_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    """Read manifest. Backfills lora/voice/history defaults if absent (Phase 9/10 forward-compat)."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("lora", _default_lora_block())
    data.setdefault("voice", _default_voice_block())
    data.setdefault("history", [])
    # Even if lora/voice exist but are partial, ensure trigger_word is locked.
    if isinstance(data.get("lora"), dict):
        data["lora"].setdefault("trigger_word", TRIGGER_WORD_LOCKED)
    return data


def write_selection(
    yaml_path: Path,
    *,
    frame_root: Path,
    selected: str,
    batch_sha8: str,
    character_card: str,
    total_spend_usd: float,
    selected_by: str | None = None,
) -> dict[str, Any]:
    """Mutate phase_8 block. Atomic. sha8 mismatch → abort without write.

    Anti-replay layer 2: recomputes sha8 from disk; rejects stale callbacks
    that reference a since-regenerated batch.
    """
    if selected not in VALID_VARIANTS:
        raise ValueError(f"selected must be one of {VALID_VARIANTS}; got {selected!r}")

    paths = sorted(Path(frame_root).glob("variant_*/*.png"))
    if len(paths) != EXPECTED_FRAME_COUNT:
        raise SelectionMismatchError(
            f"expected {EXPECTED_FRAME_COUNT} frames under {frame_root}, found {len(paths)}"
        )
    actual_sha8 = compute_batch_sha8(paths)
    if actual_sha8 != batch_sha8:
        raise SelectionMismatchError(
            f"batch_sha8 mismatch: passed={batch_sha8!r}, recomputed={actual_sha8!r}"
        )

    data = read_manifest(yaml_path)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    data["updated_at"] = now
    data["phase_8"]["status"] = "approved"
    data["phase_8"]["selected_variant"] = selected
    data["phase_8"]["selected_at"] = now
    data["phase_8"]["selected_by"] = selected_by
    data["phase_8"]["batch_sha8"] = batch_sha8
    data["phase_8"]["character_card"] = character_card
    data["phase_8"]["total_spend_usd"] = round(float(total_spend_usd), 4)
    data["history"].append(
        {
            "phase": 8,
            "event": "selected",
            "at": now,
            "note": f"Selected {selected} (sha8={batch_sha8})",
        }
    )
    _atomic_write_yaml(yaml_path, data)
    return data


def write_lora_ready(
    yaml_path: Path,
    *,
    model: str,
    version_sha256: str,
    training_run_id: str,
    trigger_word: str,
    training_dataset_size: int,
    training_cost_usd: float,
    dataset_path: str,
    training_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Phase 9 mutation: write trained LoRA audit into ``character.yaml.lora``.

    Invariants enforced:
    * ``trigger_word`` MUST equal ``TRIGGER_WORD_LOCKED`` (=``OHWX_FORTONA``);
      mismatch → ``LoraTriggerMismatchError`` BEFORE any disk write.
    * Phase 8 block and voice block are NOT mutated (additivity).
    * Atomic write via tempfile + ``os.replace`` (mirrors ``write_selection``).
    * Appends a ``history`` entry ``{phase: 9, event: lora_trained, ...}``.

    Returns the mutated manifest dict (post-write state).
    """
    if trigger_word != TRIGGER_WORD_LOCKED:
        raise LoraTriggerMismatchError(
            f"trigger_word must remain {TRIGGER_WORD_LOCKED!r}; got {trigger_word!r}"
        )

    sha = _normalize_version_sha(version_sha256)
    data = read_manifest(yaml_path)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    data["updated_at"] = now
    data["lora"] = {
        "status": "ready",
        "model": model,
        "version_sha256": sha,
        "trigger_word": TRIGGER_WORD_LOCKED,
        "training_dataset_size": int(training_dataset_size),
        "training_run_id": training_run_id,
        "training_cost_usd": round(float(training_cost_usd), 4),
        "dataset_path": dataset_path,
        "training_metadata": dict(training_metadata),
    }
    data.setdefault("history", []).append(
        {
            "phase": 9,
            "event": "lora_trained",
            "at": now,
            "note": f"{model}:{sha}",
        }
    )
    _atomic_write_yaml(yaml_path, data)
    return data


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="character.yaml writer (Phase 8 + Phase 9)")
    ap.add_argument(
        "--mode",
        choices=["selection", "lora-ready"],
        default="selection",
        help="selection (Phase 8) | lora-ready (Phase 9)",
    )
    # Phase 8 (selection) args — backward compat
    ap.add_argument("--selected", choices=list(VALID_VARIANTS))
    ap.add_argument("--batch-sha8")
    ap.add_argument(
        "--cards-file",
        help="Path to JSON file mapping {variant_id: card_text} (selection mode).",
    )
    ap.add_argument("--spend-usd", type=float)
    ap.add_argument("--frame-root", default=str(DEFAULT_FRAME_ROOT))
    ap.add_argument("--selected-by", default=None)
    # Phase 9 (lora-ready) args
    ap.add_argument("--model", help="lora-ready: <owner>/<name>, e.g. carbon1777/forton-lab-character-v1")
    ap.add_argument(
        "--version-sha256",
        help="lora-ready: bare SHA or full owner/name:sha — normalized to SHA",
    )
    ap.add_argument("--training-run-id", help="lora-ready: Replicate training.id")
    ap.add_argument("--trigger-word", default=TRIGGER_WORD_LOCKED, help="lora-ready: must equal OHWX_FORTONA")
    ap.add_argument("--training-cost-usd", type=float, help="lora-ready: actual cost from spend_tracker delta")
    ap.add_argument("--steps", type=int, help="lora-ready: training steps (metadata)")
    ap.add_argument("--rank", type=int, help="lora-ready: LoRA rank (metadata)")
    ap.add_argument("--trainer-version", help="lora-ready: ostris/flux-dev-lora-trainer version SHA")
    ap.add_argument("--dataset-size", type=int, default=30, help="lora-ready: training_dataset_size")
    ap.add_argument(
        "--dataset-path",
        default="ai_talent/dataset/v1",
        help="lora-ready: relative path to training dataset directory",
    )
    # Shared
    ap.add_argument("--yaml-path", default=str(DEFAULT_MANIFEST_PATH))
    args = ap.parse_args(argv)

    yaml_path = Path(args.yaml_path)
    if not yaml_path.exists():
        write_initial_manifest(yaml_path)

    if args.mode == "selection":
        missing = [
            n
            for n, v in [
                ("--selected", args.selected),
                ("--batch-sha8", args.batch_sha8),
                ("--cards-file", args.cards_file),
                ("--spend-usd", args.spend_usd),
            ]
            if v is None
        ]
        if missing:
            sys.stderr.write(f"ERROR: selection mode requires {missing}\n")
            return 1

        with open(args.cards_file, "r", encoding="utf-8") as f:
            cards = json.load(f)
        card = cards[args.selected]

        try:
            write_selection(
                yaml_path,
                frame_root=Path(args.frame_root),
                selected=args.selected,
                batch_sha8=args.batch_sha8,
                character_card=card,
                total_spend_usd=args.spend_usd,
                selected_by=args.selected_by,
            )
        except SelectionMismatchError as e:
            sys.stderr.write(f"REJECTED: {e}\n")
            return 2
        except (ValueError, KeyError) as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 1
        print(f"OK: {args.selected} written to {yaml_path}")
        return 0

    # mode == "lora-ready"
    missing = [
        n
        for n, v in [
            ("--model", args.model),
            ("--version-sha256", args.version_sha256),
            ("--training-run-id", args.training_run_id),
            ("--training-cost-usd", args.training_cost_usd),
            ("--steps", args.steps),
            ("--rank", args.rank),
            ("--trainer-version", args.trainer_version),
        ]
        if v is None
    ]
    if missing:
        sys.stderr.write(f"ERROR: lora-ready mode requires {missing}\n")
        return 1

    try:
        write_lora_ready(
            yaml_path,
            model=args.model,
            version_sha256=args.version_sha256,
            training_run_id=args.training_run_id,
            trigger_word=args.trigger_word,
            training_dataset_size=args.dataset_size,
            training_cost_usd=args.training_cost_usd,
            dataset_path=args.dataset_path,
            training_metadata={
                "steps": args.steps,
                "rank": args.rank,
                "trainer_version": args.trainer_version,
            },
        )
    except LoraTriggerMismatchError as e:
        sys.stderr.write(f"REJECTED: {e}\n")
        return 2
    except (ValueError, KeyError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 1
    print(f"OK: lora-ready written to {yaml_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
