# Phase 9 — LoRA Training Runbook

**Goal:** trained character-v1 LoRA + identity-anchor frozen — ready for Phase 10 (voice) and Phase 11 (pipeline scaffold).

**Total budget:** ~$3.13 (dataset $1.00 + training $2.00 + smoke $0.13).
**Actual spend (closure run):** $3.65 ($3.38 Replicate + $0.27 Anthropic monthly_plan).
**Cap:** Replicate monthly $6 (preflight enforced; mtd verified by `verify_phase9.py`).

---

## Pre-flight

1. **Replicate balance ≥ $3.** Check https://replicate.com/account/billing. The trainer charges per-second of GPU time and an unexpected reroll is the most common overage path.
2. **Git LFS installed.** First-project use needs `git lfs install`. Verify with `git lfs --version`. Dataset jpgs are LFS-tracked via `.gitattributes` (`ai_talent/dataset/v1/*.jpg`).
3. **Phase 8 closed.** Manual check:
   ```bash
   python3 -c "import yaml; d=yaml.safe_load(open('ai_talent/character.yaml')); \
     assert d['phase_8']['status']=='approved' and d['phase_8']['selected_variant']"
   ```
4. **Env vars present:**
   - `REPLICATE_API_TOKEN` (training + smoke)
   - `ANTHROPIC_API_KEY` (optional — only for caption generation if not pre-baked)

---

## Step-by-step

| Plan | Command | Time | Cost |
|------|---------|------|------|
| 09-01 | `python -m src.ai_talent.dataset_generator` + manual review | ~30 min | ~$1.00 |
| 09-02 | `python -m src.ai_talent.lora_trainer --owner <user> --input-images-url <zip-url>` | ~25–30 min | ~$2.00 |
| 09-03 | `python -c "from src.ai_talent.character_selector import write_lora_ready; write_lora_ready(...)"` | ~1 min | $0 |
| 09-04 | `python -m src.ai_talent.smoke_test` then open `ai_talent/smoke/v1/collage_1x5.png` | ~5 min | ~$0.13 |
| 09-05 | `python -m src.ai_talent.identity_anchor freeze` | ~1 min | $0 |
| 09-06 | `python scripts/verify_phase9.py` | ~10 sec | $0 |

**With pytest gate (recommended for closure):**
```bash
python scripts/verify_phase9.py --strict
```

---

## Retry paths

- **Smoke under-fit (no likeness):** re-run Plan 02 with `--steps 1500`. Roadmap allows 2 retries.
- **Smoke over-fit (all 5 frames identical):** re-roll dataset frames in Plan 01 (Bus 2 — drop top 3 most-similar), re-train Plan 02 with `--steps 1000`.
- **Profile/full-body pose fails anchor (cosine <0.85):** known limitation — Facenet512 is frontal-biased. Per-pose protocol: only `01_closeup`, `02_three_quarter`, `05_emotion` are anchor-mandatory. `03_fullbody` and `04_profile` are visual-only checks. Documented in Brain decisions 2026-05-11.
- **Teeth artefact on wide smiles:** known LoRA artefact. Mitigation in Phase 11 scriptwriter — prefer closed-mouth or subtle smile prompts.
- **DeepFace install fails on macOS arm64:** `pip install tensorflow-macos tensorflow-metal` first (or fall back to `cpu`-only TensorFlow).

---

## Closure

```bash
python scripts/verify_phase9.py --strict
# exit 0 → all 5 gates GREEN → Phase 9 closed.
```

Append Brain decisions snippet (already done at closure):
> **2026-05-11 — Phase 9 closed: character-v1 LoRA trained**
> Model: `carbon1777/forton-lab-character-v1:5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1`
> Spend: $3.65. Anchor frozen (Facenet512, threshold 0.85).
> Phase 10 (voice) + Phase 11 (pipeline scaffold) unblocked.

---

## What's where

| Artefact | Path | Provenance |
|---|---|---|
| Dataset (30 frames + captions + MANIFEST) | `ai_talent/dataset/v1/` | Phase 9-01, PR #35 |
| LoRA trainer module | `src/ai_talent/lora_trainer.py` | Phase 9-02, PR #37 |
| Daily-cap override util | `src/spend_tracker_v2.py` (override flag) | Phase 9-02, PR #38 |
| `character.yaml.lora.status=ready` | `ai_talent/character.yaml` | Phase 9-03, PR #42 |
| Smoke test code + 5 PNGs + collage | `src/ai_talent/smoke_test.py`, `ai_talent/smoke/v1/` | Phase 9-04, PR #43 |
| Identity anchor (Facenet512) | `src/ai_talent/identity_anchor.py`, `ai_talent/anchor/v1/anchor.json` | Phase 9-05, PR #44 |
| Acceptance gate (this runbook) | `scripts/verify_phase9.py`, `RUNBOOK_PHASE9.md` | Phase 9-06 (this PR) |

---

## Re-running Phase 9 from scratch (CHAR-V2 in v1.2)

1. Reroll dataset: `rm -rf ai_talent/dataset/v2 && python -m src.ai_talent.dataset_generator --version v2`
2. Re-train: pass `--input-images-url` of the new v2 zip; new model gets a new SHA.
3. Reset `character.yaml.lora.status` to `pending` and re-run Plans 03 → 04 → 05 with v2 paths.
4. `python scripts/verify_phase9.py` will compare anchor SHA against character.yaml SHA — mismatch fails CHAR-06.
