# Identity Anchor v1 — Forton Lab studio mascot character-v1

Frozen on **2026-05-10T23:19:13Z** from `ai_talent/smoke/v1/*.png` (5 LOCKED prompts, Plan 09-04).
LoRA referenced: `carbon1777/forton-lab-character-v1:5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1`.
Threshold: cosine ≥ **0.85** (PITFALLS.md P7 / CHAR-06).

Model: **Facenet512** (DeepFace 0.0.100). Detector: **retinaface**.

## Verifying a future v2 LoRA

1. Train v2 LoRA via Phase 9 process (or v1.2 CHAR-V2).
2. Re-run smoke with v2 using the SAME 5 prompts in `ai_talent/smoke/v1/anchor_prompts.txt`
   (DO NOT change them — those are the frozen fixture).
3. For each smoke PNG produced by v2:
   ```bash
   .venv-face/bin/python -m src.ai_talent.identity_anchor verify \
     --candidate ai_talent/smoke/v2/<name>.png
   ```
4. **Recommended threshold profile** — see "Per-pose sanity" below. The global `0.85` mean-comparison
   gate is strict; in practice we observe close-up frames PASS at ≥0.85 while full-body/profile
   frames have lower cosines against the mean because Facenet512 weighs frontal close-ups much
   higher. For Phase 11 v2 verification consider per-frame comparison (cosine vs the same-pose
   v1 embedding) instead of vs mean.

## Per-pose sanity (v1 against its own mean) — 2026-05-11

| Frame | cosine vs mean | Verdict at 0.85 |
|---|---|---|
| 01_closeup.png | 0.9105 | PASS |
| 02_three_quarter.png | 0.8565 | PASS |
| 03_fullbody.png | 0.7813 | FAIL |
| 04_profile.png | 0.5181 | FAIL |
| 05_emotion.png | 0.8555 | PASS |

This is **expected behaviour**, not a v1 LoRA defect. Facenet512 is trained on frontal close-ups;
profile and full-body shots have less face surface area for the model to weigh. The 3/5 frontal-ish
frames all PASS comfortably. Full-body and profile are tracked but should be verified against
**their own pose-matched v1 embedding**, not the global mean.

**For Phase 11 v2 verification protocol:** use the individual `embeddings[<pose>.png]` from
`anchor.json` instead of `mean_embedding` when comparing the corresponding v2 frame. This avoids
the pose-mismatch penalty.

## Files

- `anchor.json` — embeddings + threshold + LoRA reference (Facenet512 / retinaface), 5×512-dim
  embeddings + precomputed mean (81 KB).
- `README.md` — this file.

## Environment

Requires Python 3.12 venv (`.venv-face`). TensorFlow has no Python 3.14 wheel; the rest of the
pipeline runs in `.venv` on 3.14. `tf-keras` is required because TF 2.16+ uses Keras 3 and
retina-face/deepface need the legacy Keras 2 shim.

```bash
python3.12 -m venv .venv-face
.venv-face/bin/pip install deepface==0.0.100 tf-keras
.venv-face/bin/python -m src.ai_talent.identity_anchor freeze
```
