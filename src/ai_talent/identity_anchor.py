"""Phase 9 identity anchor — Facenet512 embeddings + cosine ≥0.85 (PITFALLS P7 / CHAR-06).

freeze_v1: compute embeddings on Plan 04 smoke outputs → ai_talent/anchor/v1/anchor.json.
verify_identity: load anchor, compute candidate embedding, cosine vs mean → bool.

This is the mechanical defense against character drift. Without a frozen embedding
baseline, "v2 looks similar enough" is unfalsifiable. With it, we have an objective
gate before any LoRA replacement.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import yaml

MODEL_NAME = "Facenet512"
DETECTOR_BACKEND = "retinaface"
DEFAULT_THRESHOLD = 0.85
DEFAULT_SMOKE_DIR = Path("ai_talent/smoke/v1")
DEFAULT_ANCHOR_DIR = Path("ai_talent/anchor/v1")
DEFAULT_YAML_PATH = Path("ai_talent/character.yaml")
_EXCLUDED_PNGS = {"collage_1x5.png"}


def _get_embedding(image_path: Path) -> list[float]:
    """Compute Facenet512 embedding via retinaface detector. Returns 512-dim list."""
    # Lazy import — heavy TF/Keras tower, only loaded when actually computing.
    from deepface import DeepFace

    res = DeepFace.represent(
        img_path=str(image_path),
        model_name=MODEL_NAME,
        detector_backend=DETECTOR_BACKEND,
        enforce_detection=True,
    )
    return [float(x) for x in res[0]["embedding"]]


def cosine_similarity(a, b) -> float:
    """Cosine similarity in [-1, 1]; returns 0.0 for zero-norm vectors."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def freeze_v1(
    smoke_dir: Path = DEFAULT_SMOKE_DIR,
    anchor_dir: Path = DEFAULT_ANCHOR_DIR,
    character_yaml_lora: dict[str, str] | None = None,
) -> Path:
    """Compute embeddings on 5 smoke PNGs → anchor.json with mean_embedding + threshold."""
    pngs = sorted(p for p in smoke_dir.glob("*.png") if p.name not in _EXCLUDED_PNGS)
    if len(pngs) < 5:
        raise RuntimeError(
            f"need ≥5 smoke PNGs in {smoke_dir}; found {len(pngs)} "
            f"(after excluding {_EXCLUDED_PNGS})"
        )
    embeddings = {p.name: _get_embedding(p) for p in pngs[:5]}
    mean_emb = [float(x) for x in np.mean(np.array(list(embeddings.values())), axis=0)]

    anchor_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "model": MODEL_NAME,
        "detector": DETECTOR_BACKEND,
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "character_yaml_lora": character_yaml_lora or {},
        "threshold": DEFAULT_THRESHOLD,
        "embeddings": embeddings,
        "mean_embedding": mean_emb,
    }
    out = anchor_dir / "anchor.json"
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return out


def verify_identity(
    candidate: Path,
    *,
    anchor_path: Path = DEFAULT_ANCHOR_DIR / "anchor.json",
    threshold: float | None = None,
) -> bool:
    """True iff cosine(candidate_emb, mean(anchor_embs)) ≥ threshold (default 0.85)."""
    data = json.loads(Path(anchor_path).read_text(encoding="utf-8"))
    thr = threshold if threshold is not None else float(data.get("threshold", DEFAULT_THRESHOLD))
    cand = _get_embedding(Path(candidate))
    mean = data.get("mean_embedding")
    if not mean:
        mean = [float(x) for x in np.mean(np.array(list(data["embeddings"].values())), axis=0)]
    sim = cosine_similarity(cand, mean)
    return sim >= thr


def verify_identity_with_score(
    candidate: Path,
    *,
    anchor_path: Path = DEFAULT_ANCHOR_DIR / "anchor.json",
    threshold: float | None = None,
) -> tuple[bool, float]:
    """Same as verify_identity but also returns the actual cosine score for diagnostics."""
    data = json.loads(Path(anchor_path).read_text(encoding="utf-8"))
    thr = threshold if threshold is not None else float(data.get("threshold", DEFAULT_THRESHOLD))
    cand = _get_embedding(Path(candidate))
    mean = data.get("mean_embedding") or [
        float(x) for x in np.mean(np.array(list(data["embeddings"].values())), axis=0)
    ]
    sim = cosine_similarity(cand, mean)
    return sim >= thr, sim


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase 9 identity anchor — DeepFace Facenet512")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("freeze", help="Compute embeddings on smoke PNGs → anchor.json")
    f.add_argument("--smoke-dir", default=str(DEFAULT_SMOKE_DIR))
    f.add_argument("--anchor-dir", default=str(DEFAULT_ANCHOR_DIR))
    f.add_argument("--yaml-path", default=str(DEFAULT_YAML_PATH))

    v = sub.add_parser("verify", help="Check candidate face against anchor")
    v.add_argument("--candidate", required=True)
    v.add_argument("--anchor-path", default=str(DEFAULT_ANCHOR_DIR / "anchor.json"))
    v.add_argument("--threshold", type=float, default=None)

    args = ap.parse_args(argv)

    if args.cmd == "freeze":
        with open(args.yaml_path) as fh:
            yd = yaml.safe_load(fh)
        lora_ref = {
            "model": yd["lora"]["model"],
            "version_sha256": yd["lora"]["version_sha256"],
        }
        out = freeze_v1(Path(args.smoke_dir), Path(args.anchor_dir), lora_ref)
        print(f"frozen: {out}")
        return 0

    if args.cmd == "verify":
        ok, sim = verify_identity_with_score(
            Path(args.candidate),
            anchor_path=Path(args.anchor_path),
            threshold=args.threshold,
        )
        print(f"{'PASS' if ok else 'FAIL'}  cosine={sim:.4f}")
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
