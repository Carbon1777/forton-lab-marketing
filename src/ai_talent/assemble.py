"""Phase 11 Stage orchestrator (PIPE-02).

CLI: ``python -m src.ai_talent.assemble --brief <path> --slug <slug>
       [--ltx-density B|A|C] [--from-stage 1..7] [--force-stage <name>]``

Wires Plans 02-05 modules through pipeline_cache.run_stage. Final mp4 lives
in ``assets/video/test/<slug>.mp4`` (Phase 11). Phase 12 wires queue handoff.

Scenario routing:
  * B (default): LTX hero only on the single is_hero=true beat (~$0.40)
  * A: LTX on every beat (~$1.92 for 6 beats × 4 sec)
  * C: no LTX; hero rendered as Ken Burns static like other beats (~$0)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import frontmatter
import yaml

from src.spend_tracker_v2 import preflight_check, record_provider_spend
from src.ai_talent import (
    preflight,
    pipeline_cache,
    script_builder,
    frame_renderer,
    voice_synth,
    srt_builder,
    video_compositor,
    bitrate_fitter,
    _ltx_api,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_CHARACTER_YAML: Final[Path] = _REPO_ROOT / "ai_talent" / "character.yaml"
DEFAULT_CACHE_ROOT: Final[Path] = _REPO_ROOT / ".cache"
DEFAULT_FINAL_DIR: Final[Path] = _REPO_ROOT / "assets" / "video" / "test"

SLUG_RE: Final[re.Pattern] = re.compile(r"^[a-z0-9-]+$")
TARGET_MB: Final[float] = 18.0
BITRATE_AUDIO_KBPS: Final[int] = 96

LTX_MODEL: Final[str] = "ltx-2-3-pro"
LTX_RESOLUTION: Final[str] = "1080x1920"
LTX_FPS: Final[int] = 24

STAGE_NAMES: Final[tuple[str, ...]] = (
    "script", "frames", "voice", "ltx", "srt", "composite", "bitrate_fit",
)


class AssembleError(RuntimeError):
    """Raised on preflight red, slug invalid, missing character_card, etc."""


def _sha256(content: bytes | str) -> str:
    h = hashlib.sha256()
    h.update(content.encode("utf-8") if isinstance(content, str) else content)
    return h.hexdigest()


def _resolve_character_card(char_yaml_path: Path) -> str:
    """Read character.yaml → return phase_8.character_card string.

    Raises AssembleError if file missing, unparseable, or card empty.
    """
    if not char_yaml_path.exists():
        raise AssembleError(f"character.yaml missing: {char_yaml_path}")
    try:
        data = yaml.safe_load(char_yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise AssembleError(f"character.yaml unparseable: {e}") from e
    card = ((data.get("phase_8") or {}).get("character_card") or "").strip()
    if not card:
        raise AssembleError("character.yaml.phase_8.character_card empty")
    return card


def _ltx_call_via_BOOT_01(
    *,
    prompt: str,
    out_path: Path,
    duration_sec: int,
    image_path: Path | None,
    spend_file: Path,
) -> Path:
    """LTX call wrapped by BOOT-01 4-step (Plan 03 _ltx_api is a thin client).

    1. estimate_cost → preflight_check (raises if over cap)
    2. _ltx_api.generate(...) → bytes
    3. write to out_path
    4. record_provider_spend("ltx", units=duration_sec, unit_field="seconds")
    """
    est_cost = _ltx_api.estimate_cost(LTX_MODEL, duration_sec, LTX_RESOLUTION)
    preflight_check(spend_file, "ltx", est_cost)
    video_bytes = _ltx_api.generate(
        prompt=prompt,
        duration_sec=duration_sec,
        model=LTX_MODEL,
        resolution=LTX_RESOLUTION,
        fps=LTX_FPS,
        generate_audio=False,
        image_path=str(image_path) if image_path else None,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(video_bytes)
    record_provider_spend(
        spend_file, "ltx",
        usd=est_cost,
        units=duration_sec, unit_field="seconds",
    )
    return out_path


def assemble(
    *,
    brief_path: Path,
    slug: str,
    ltx_density: str = "B",
    from_stage: int = 1,
    force_stage: str | None = None,
    spend_file: Path = DEFAULT_SPEND_FILE,
    char_yaml_path: Path = DEFAULT_CHARACTER_YAML,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    final_dir: Path = DEFAULT_FINAL_DIR,
    run_preflight: bool = True,
) -> dict:
    """Run the 7-stage pipeline.

    Returns ``{slug, out_path, size_mb, duration_sec, ltx_density}``.
    """
    if not SLUG_RE.match(slug):
        raise AssembleError(
            f"invalid slug {slug!r} (must match {SLUG_RE.pattern})"
        )
    if ltx_density not in {"A", "B", "C"}:
        raise AssembleError(
            f"invalid ltx_density={ltx_density!r} (must be A, B, or C)"
        )
    if force_stage is not None and force_stage not in STAGE_NAMES:
        raise AssembleError(
            f"invalid force_stage={force_stage!r} (must be one of {STAGE_NAMES})"
        )

    if run_preflight:
        ok, results = preflight.run_checks()
        if not ok:
            fail = "; ".join(
                f"{r['check']}={r['msg']}"
                for r in results if not r["pass"]
            )
            raise AssembleError(f"preflight RED: {fail}")

    # === STAGE 1 — script (Anthropic tools API) ===
    brief_post = frontmatter.load(brief_path)
    brief_md = brief_post.content
    character_card = _resolve_character_card(char_yaml_path)
    product = brief_post.metadata.get("product", "centry")
    script_hash = _sha256(brief_md + character_card)
    force_1 = force_stage == "script"

    def _run_script(stage_dir: Path) -> None:
        script_builder.build_script(
            brief_md=brief_md,
            character_card=character_card,
            out_path=stage_dir / "script.json",
            spend_file=spend_file,
        )

    script_path = pipeline_cache.run_stage(
        slug=slug, stage_num=1, name="script",
        inputs_for_hash=script_hash, output_marker="script.json",
        run_fn=_run_script, cache_root=cache_root, force=force_1,
    )
    script = json.loads(script_path.read_text(encoding="utf-8"))
    beats = script["beats"]
    voice_lines = script["voice_lines"]
    hero_id = script["hero_beat_id"]

    # === STAGE 2 — frames (Replicate Flux+LoRA, 1 per beat) ===
    frames_hash = _sha256(json.dumps([b["frame_prompt"] for b in beats]))
    force_2 = force_stage == "frames"

    def _run_frames(stage_dir: Path) -> None:
        for n, beat in enumerate(beats, 1):
            frame_renderer.render_frame(
                beat["frame_prompt"],
                stage_dir / f"frame_{n:02d}.png",
                char_yaml_path=char_yaml_path,
                spend_file=spend_file,
                seed=n,
            )

    frame_01 = pipeline_cache.run_stage(
        slug=slug, stage_num=2, name="frames",
        inputs_for_hash=frames_hash, output_marker="frame_01.png",
        run_fn=_run_frames, cache_root=cache_root, force=force_2,
    )
    frames_dir = frame_01.parent

    # === STAGE 3 — voice (ElevenLabs TTS) ===
    voice_hash = _sha256(json.dumps([vl["text"] for vl in voice_lines]))
    force_3 = force_stage == "voice"

    def _run_voice(stage_dir: Path) -> None:
        for n, vl in enumerate(voice_lines, 1):
            voice_synth.synthesize_line(
                vl["text"],
                stage_dir / f"line_{n:02d}.mp3",
                product=product,
                char_yaml_path=char_yaml_path,
                spend_file=spend_file,
            )

    line_01 = pipeline_cache.run_stage(
        slug=slug, stage_num=3, name="voice",
        inputs_for_hash=voice_hash, output_marker="line_01.mp3",
        run_fn=_run_voice, cache_root=cache_root, force=force_3,
    )
    voice_dir = line_01.parent

    # === STAGE 4 — LTX (scenario-routed) ===
    ltx_outputs: list[Path | None] = [None] * len(beats)
    if ltx_density in {"A", "B"}:
        if ltx_density == "A":
            ltx_targets = list(beats)
        else:  # B
            hero_beat = next((b for b in beats if b["id"] == hero_id), None)
            if hero_beat is None:
                raise AssembleError(
                    f"hero_beat_id={hero_id!r} not found in beats"
                )
            ltx_targets = [hero_beat]

        ltx_hash = _sha256(json.dumps([
            {"id": b["id"], "p": b["frame_prompt"],
             "d": b["duration_sec"]}
            for b in ltx_targets
        ]))
        force_4 = force_stage == "ltx"
        # Use the FIRST target's id as the output_marker — guaranteed to exist
        # whether density A (first beat) or B (hero beat).
        marker = f"ltx_{ltx_targets[0]['id']}.mp4"

        def _run_ltx(stage_dir: Path) -> None:
            for b in ltx_targets:
                idx = beats.index(b)
                frame_path = frames_dir / f"frame_{idx + 1:02d}.png"
                _ltx_call_via_BOOT_01(
                    prompt=b["frame_prompt"],
                    out_path=stage_dir / f"ltx_{b['id']}.mp4",
                    duration_sec=int(b["duration_sec"]),
                    image_path=frame_path if frame_path.exists() else None,
                    spend_file=spend_file,
                )

        ltx_first = pipeline_cache.run_stage(
            slug=slug, stage_num=4, name="ltx",
            inputs_for_hash=ltx_hash, output_marker=marker,
            run_fn=_run_ltx, cache_root=cache_root, force=force_4,
        )
        ltx_dir = ltx_first.parent
        for b in ltx_targets:
            idx = beats.index(b)
            ltx_outputs[idx] = ltx_dir / f"ltx_{b['id']}.mp4"

    # === STAGE 5 — SRT ===
    ts_paths = [voice_dir / f"line_{n:02d}.timestamps.json"
                for n in range(1, len(voice_lines) + 1)]
    beat_by_id = {b["id"]: b for b in beats}
    line_durations: list[float] = [
        float(beat_by_id[vl["beat_id"]]["duration_sec"])
        for vl in voice_lines
    ]
    line_offsets: list[float] = []
    running = 0.0
    for d in line_durations:
        line_offsets.append(running)
        running += d
    srt_hash = _sha256(json.dumps([
        {"o": o, "d": d}
        for o, d in zip(line_offsets, line_durations)
    ]))
    force_5 = force_stage == "srt"

    def _run_srt(stage_dir: Path) -> None:
        srt_builder.build_srt(
            timestamps_paths=ts_paths,
            voice_line_texts=[vl["text"] for vl in voice_lines],
            out_path=stage_dir / "captions.srt",
            line_offsets_sec=line_offsets,
            audio_durations_sec=line_durations,
        )

    srt_path = pipeline_cache.run_stage(
        slug=slug, stage_num=5, name="srt",
        inputs_for_hash=srt_hash, output_marker="captions.srt",
        run_fn=_run_srt, cache_root=cache_root, force=force_5,
    )

    # === STAGE 6 — composite (Ken Burns + LTX segments + concat + mux + burn) ===
    composite_hash = _sha256(json.dumps({
        "beats": [b["id"] for b in beats],
        "ltx_density": ltx_density,
    }))
    force_6 = force_stage == "composite"

    def _run_composite(stage_dir: Path) -> None:
        # 6a. per-beat segment (Ken Burns OR normalized LTX clip)
        segments: list[Path] = []
        for i, beat in enumerate(beats):
            seg = stage_dir / f"seg_{i:02d}.mp4"
            ltx_clip = ltx_outputs[i]
            if ltx_clip is not None and ltx_clip.exists():
                # Normalize LTX clip to 25fps libx264 yuv420p to match Ken
                # Burns segments (Pitfall 7 — concat demuxer needs identical
                # stream params).
                video_compositor._run([
                    "-i", str(ltx_clip),
                    "-vf", "fps=25,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-r", "25",
                    "-an",
                    "-movflags", "+faststart",
                    str(seg),
                ])
            else:
                video_compositor.ken_burns(
                    frames_dir / f"frame_{i + 1:02d}.png",
                    float(beat["duration_sec"]),
                    seg,
                )
            segments.append(seg)
        # 6b. concat
        concat_out = stage_dir / "_concat.mp4"
        video_compositor.concat_segments(segments, concat_out)
        # 6c. mux audio
        voice_mp3s = [voice_dir / f"line_{n:02d}.mp3"
                      for n in range(1, len(voice_lines) + 1)]
        mux_out = stage_dir / "_mux.mp4"
        video_compositor.mux_audio(concat_out, voice_mp3s, mux_out)
        # 6d. burn subtitles
        final_raw = stage_dir / "composite.mp4"
        video_compositor.burn_subtitles(mux_out, srt_path, final_raw)

    composite_path = pipeline_cache.run_stage(
        slug=slug, stage_num=6, name="composite",
        inputs_for_hash=composite_hash, output_marker="composite.mp4",
        run_fn=_run_composite, cache_root=cache_root, force=force_6,
    )

    # === STAGE 7 — bitrate fit (2-pass libx264 to ≤18 МБ) ===
    final_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = final_dir / f"{slug}.mp4"
    bitrate_fitter.fit_to_size(
        composite_path, out_mp4,
        target_mb=TARGET_MB, audio_kbps=BITRATE_AUDIO_KBPS,
    )

    # ffprobe summary for the return dict
    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:format=size",
         "-of", "json", str(out_mp4)],
        capture_output=True, text=True, check=False,
    )
    duration_sec = 0.0
    if getattr(probe, "stdout", ""):
        try:
            info = json.loads(probe.stdout)["format"]
            duration_sec = float(info.get("duration", 0))
        except Exception:
            duration_sec = 0.0
    try:
        size_mb = out_mp4.stat().st_size / (1024 * 1024)
    except FileNotFoundError:
        size_mb = 0.0
    return {
        "slug": slug,
        "out_path": str(out_mp4),
        "size_mb": round(size_mb, 2),
        "duration_sec": round(duration_sec, 2),
        "ltx_density": ltx_density,
    }


def _cli(argv: list[str] | None = None) -> int:
    """CLI entry-point. Returns 0 on success, 1 on AssembleError."""
    ap = argparse.ArgumentParser(
        description="Phase 11 video assembler (PIPE-02)"
    )
    ap.add_argument("--brief", required=True, type=Path,
                    help="Path to markdown brief with YAML frontmatter")
    ap.add_argument("--slug", required=True,
                    help="Output slug (regex ^[a-z0-9-]+$)")
    ap.add_argument("--ltx-density", choices=["A", "B", "C"], default="B",
                    help="A=every beat, B=hero only (default), C=no LTX")
    ap.add_argument("--from-stage", type=int, default=1,
                    choices=list(range(1, 8)),
                    help="Resume at stage 1..7 (1=script, 7=bitrate_fit)")
    ap.add_argument("--force-stage",
                    choices=list(STAGE_NAMES), default=None,
                    help="Invalidate one stage's cache before running")
    ap.add_argument("--no-preflight", action="store_true",
                    help="Skip preflight (DANGEROUS — tests only)")
    args = ap.parse_args(argv)

    if not SLUG_RE.match(args.slug):
        ap.error(f"invalid slug {args.slug!r} (must match {SLUG_RE.pattern})")

    try:
        result = assemble(
            brief_path=args.brief, slug=args.slug,
            ltx_density=args.ltx_density,
            from_stage=args.from_stage,
            force_stage=args.force_stage,
            run_preflight=not args.no_preflight,
        )
    except AssembleError as e:
        print(f"ASSEMBLE FAIL: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())


__all__ = [
    "AssembleError",
    "assemble",
    "_cli",
    "_resolve_character_card",
    "_ltx_call_via_BOOT_01",
    "SLUG_RE",
    "STAGE_NAMES",
    "TARGET_MB",
    "LTX_MODEL",
    "LTX_RESOLUTION",
]
