---
# ----------------------------------------------------------------------
# Phase 11 brief frontmatter contract.
# Plan 02 (script_builder) reads these keys to build script.json.
# Required: product, topic, hook, cta.
# Optional: series, episode, ltx_density override, duration_target_sec.
# ----------------------------------------------------------------------
product: centry              # centry | diktum — chooses voice_settings preset
topic: "<one-sentence theme of the video>"
hook: "<3-4 word overlay text shown in the first 3 seconds>"
cta: "<final call-to-action line shown over outro frame>"

# Series flag — when true, voice_synth applies series intro/outro slot.
series_flag: false
series: null                 # optional: "S01"
episode: null                # optional: "E03"

# LTX hero-shot density. Defaults to scenario B (hero-only).
#   A = all-LTX (every frame animated)              ~$1.20/ролик
#   B = hero-only (one 5-sec animated cut)          ~$0.40/ролик  (default)
#   C = zero-LTX (static frames only, no LTX call)  ~$0.00/ролик
ltx_density: B

# Hard cap enforced by script_builder. Phase 11 invariant: 28-32 sec.
duration_target_sec: 30
---

# Brief body

Free-form Russian text describing what the video should communicate,
plus optional production notes (camera angle hints, lighting mood, etc.).

script_builder reads frontmatter + this body and emits structured `script.json`
matching the schema in `.planning/phases/11-pipeline-scaffolding/11-PATTERNS.md`.

## Notes for operator

- Do not duplicate frontmatter content here — `topic` / `hook` / `cta`
  are the structured source of truth, body is for tone and texture.
- Keep body under ~400 words; Anthropic input tokens count toward the
  $0.20 budget per script generation.
