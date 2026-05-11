# Phase 11: Pipeline Scaffolding — Operator Runbook

Phase 11 ships the end-to-end AI-talent video pipeline:
`brief.md → script.json → frames + voice + LTX hero → ffmpeg composite → MP4 ≤18 МБ`.

## Overview

_Filled by Plan 07 once Wave 1-3 actuals are known._

Stub: full operator flow lives in the brief at
`/Users/jcat/Documents/Forton Lab/marketing-v3/MARKETING_PIPELINE_V4_BRIEF.md`
until this runbook is completed.

## Pipeline Stages

1. **Stage 1 (`script_builder`):** Anthropic tools API → `script.json`
2. **Stage 2 (`frame_renderer`):** Replicate Flux + LoRA → `frame_NN.png`
3. **Stage 3 (`voice_synth`):** ElevenLabs → `line_NN.mp3` + timestamps
4. **Stage 4 (`ltx_hero`):** LTX text/image-to-video → `hero.mp4` (scenarios A/B only)
5. **Stage 5 (`srt_builder`):** timestamps → `captions.srt`
6. **Stage 6 (`compositor`):** ffmpeg primitives → `final_raw.mp4`
7. **Stage 7 (`bitrate_fitter`):** 2-pass libx264 → `<slug>.mp4` ≤18 МБ

## Recovery Paths

_Filled by Plan 07 from accumulated W-NNN tickets._

Pre-locked entries:

### W-001 (Phase 9 carry-forward) — preflight/record gap in `lora_trainer.train_v1`

If a LoRA training run was killed between `trainings.create` and `status == "succeeded"`,
the $2.20 spend was NOT recorded. Manual recovery:

```bash
cd /tmp/mv3-phase9-bb1559
python -c "
from pathlib import Path
from src.spend_tracker_v2 import record_provider_spend
record_provider_spend(Path('.metrics/api_spend.json'), 'replicate',
                      usd=2.20, units=1800, unit_field='predict_seconds')
"
```

Regression test guards this hook: `tests/test_phase9_w001_carry_forward.py`.

## API Cost Reference

_Filled by Plan 07 (Phase 11 spent actuals)._

Pre-locked expectations (per-render budget):
- Anthropic script generation: ~$0.05
- Replicate Flux + LoRA frames (8 frames): ~$0.30
- ElevenLabs TTS (30 sec): ~$0.00 (Starter quota)
- LTX hero (scenario B, 5 sec): ~$0.40

Total per-render target: **~$0.75** (cap ~$0.85 with overhead).
Monthly abort cap: **$15** (BOOT-01 enforced).
