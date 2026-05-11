"""Forton Lab AI-talent subpackage (Phase 8 → 11).

Public modules wired into the Phase 11 pipeline:
    * preflight       — 5 health checks (PIPE-04)
    * assemble        — CLI orchestrator stitching all 7 stages (PIPE-02)
    * pipeline_cache  — hash-and-skip cache foundation
    * script_builder  — Stage 1 (Anthropic tools API)
    * frame_renderer  — Stage 2 (Replicate Flux+LoRA)
    * voice_synth     — Stage 3 (ElevenLabs TTS)
    * srt_builder     — Stage 5 (SRT)
    * video_compositor / bitrate_fitter — Stage 6+7 (ffmpeg)
    * _ltx_api        — Stage 4 LTX hero clip (BOOT-01 wrap at caller)
"""

__all__ = [
    "assemble",
    "preflight",
    "pipeline_cache",
    "script_builder",
    "frame_renderer",
    "voice_synth",
    "srt_builder",
    "video_compositor",
    "bitrate_fitter",
    "_ltx_api",
]
