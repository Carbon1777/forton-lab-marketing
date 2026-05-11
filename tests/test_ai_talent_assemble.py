"""Phase 11-06 — assemble.py orchestrator unit tests (PIPE-02).

All stages mocked. The LIVE smoke test is Plan 06 Task 3 — out of scope here.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.ai_talent import assemble


# -- slug validation ----------------------------------------------------------

def test_slug_validation_accepts_canonical_slug(tmp_path):
    """test-centry-smoke passes the regex."""
    assert assemble.SLUG_RE.match("test-centry-smoke")
    assert assemble.SLUG_RE.match("centry-001")
    assert assemble.SLUG_RE.match("a")


def test_slug_validation_rejects_uppercase():
    assert not assemble.SLUG_RE.match("Test-Slug")
    assert not assemble.SLUG_RE.match("CENTRY")


def test_slug_validation_rejects_underscore():
    assert not assemble.SLUG_RE.match("test_slug")


def test_slug_validation_rejects_path_traversal():
    assert not assemble.SLUG_RE.match("../foo")
    assert not assemble.SLUG_RE.match("a/b")
    assert not assemble.SLUG_RE.match("a.b")


def test_assemble_raises_on_bad_slug(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("---\nproduct: centry\n---\nx\n", encoding="utf-8")
    with pytest.raises(assemble.AssembleError, match="invalid slug"):
        assemble.assemble(brief_path=brief, slug="BAD_SLUG", run_preflight=False)


def test_assemble_raises_on_bad_density(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("---\nproduct: centry\n---\nx\n", encoding="utf-8")
    with pytest.raises(assemble.AssembleError, match="ltx_density"):
        assemble.assemble(brief_path=brief, slug="good-slug",
                          ltx_density="Z", run_preflight=False)


# -- preflight gate -----------------------------------------------------------

def test_assemble_aborts_when_preflight_red(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("---\nproduct: centry\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.ai_talent.assemble.preflight.run_checks",
        lambda: (False, [{"check": "replicate", "pass": False,
                          "msg": "missing"}]),
    )
    with pytest.raises(assemble.AssembleError, match="preflight RED"):
        assemble.assemble(brief_path=brief, slug="x", run_preflight=True)


# -- character.yaml gate ------------------------------------------------------

def test_resolve_character_card_returns_card(tmp_path):
    f = tmp_path / "char.yaml"
    f.write_text(yaml.safe_dump({
        "phase_8": {"character_card": "A 26-year-old…"},
    }), encoding="utf-8")
    assert assemble._resolve_character_card(f).startswith("A 26-year-old")


def test_resolve_character_card_raises_on_empty(tmp_path):
    f = tmp_path / "char.yaml"
    f.write_text(yaml.safe_dump({"phase_8": {}}), encoding="utf-8")
    with pytest.raises(assemble.AssembleError, match="character_card"):
        assemble._resolve_character_card(f)


# -- end-to-end mocked --------------------------------------------------------

@pytest.fixture
def mock_brief_and_yaml(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text(
        "---\nslug: test-x\nproduct: centry\n---\nBody copy of brief\n",
        encoding="utf-8",
    )
    char_yaml = tmp_path / "character.yaml"
    char_yaml.write_text(yaml.safe_dump({
        "phase_8": {"character_card": "A 26-year-old Russian woman..."},
        "lora": {"status": "ready", "model": "x/y",
                 "version_sha256": "z" * 64},
        "voice": {"status": "ready", "voice_id": "vid"},
    }), encoding="utf-8")
    return brief, char_yaml


def _fake_script_json() -> dict:
    """Build a 4-beat script with hero_beat=b2."""
    return {
        "hook": "Hook line",
        "product": "centry",
        "series_flag": None,
        "hero_beat_id": "b2",
        "cuts": [],
        "cta": "Download Centry",
        "beats": [
            {"id": f"b{i}", "frame_prompt": f"OHWX_FORTONA scene {i}",
             "duration_sec": 7.0, "is_hero": (i == 2)}
            for i in (1, 2, 3, 4)
        ],
        "voice_lines": [
            {"beat_id": f"b{i}", "text": f"Line {i} of Russian voice text."}
            for i in (1, 2, 3, 4)
        ],
    }


def _patch_stages(monkeypatch, tmp_path, ltx_called: list,
                   density_b_default=True) -> dict:
    """Patch every external module the orchestrator touches.

    Returns dict of MagicMocks for assertion convenience.
    """
    monkeypatch.setattr(
        "src.ai_talent.assemble.preflight.run_checks",
        lambda: (True, []),
    )

    # Pipeline cache: redirect to tmp_path-relative dir + actually invoke run_fn
    # so the orchestrator's stage handlers run.
    def fake_run_stage(*, slug, stage_num, name, inputs_for_hash, output_marker,
                       run_fn, cache_root=None, force=False):
        stage_dir = (tmp_path / ".cache" / slug /
                     f"{stage_num:02d}-{name}")
        stage_dir.mkdir(parents=True, exist_ok=True)
        out = stage_dir / output_marker
        if not out.exists():
            run_fn(stage_dir)
        return out

    monkeypatch.setattr("src.ai_talent.assemble.pipeline_cache.run_stage",
                        fake_run_stage)

    # --- script_builder.build_script: write fake script.json ---
    def fake_build_script(*, brief_md, character_card, out_path,
                          spend_file=None):
        out_path.write_text(json.dumps(_fake_script_json()), encoding="utf-8")
        return out_path

    monkeypatch.setattr("src.ai_talent.assemble.script_builder.build_script",
                        fake_build_script)

    # --- frame_renderer.render_frame: write a 1-byte png placeholder ---
    def fake_render_frame(prompt, out_path, *, char_yaml_path=None,
                          spend_file=None, seed=None):
        out_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return out_path

    monkeypatch.setattr("src.ai_talent.assemble.frame_renderer.render_frame",
                        fake_render_frame)

    # --- voice_synth.synthesize_line: write mp3 + timestamps.json ---
    def fake_synthesize_line(text, out_mp3_path, *, product=None,
                             char_yaml_path=None, spend_file=None):
        out_mp3_path.write_bytes(b"mp3")
        ts = out_mp3_path.with_suffix(".timestamps.json")
        ts.write_text(json.dumps({"fallback": True, "text": text}),
                      encoding="utf-8")
        return out_mp3_path

    monkeypatch.setattr("src.ai_talent.assemble.voice_synth.synthesize_line",
                        fake_synthesize_line)

    # --- srt_builder.build_srt: write trivial srt ---
    def fake_build_srt(*, timestamps_paths, voice_line_texts, out_path,
                       line_offsets_sec=None, audio_durations_sec=None):
        out_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n",
                            encoding="utf-8")
        return out_path

    monkeypatch.setattr("src.ai_talent.assemble.srt_builder.build_srt",
                        fake_build_srt)

    # --- video_compositor primitives ---
    def fake_ken_burns(image, duration_sec, out, fps=25):
        out.write_bytes(b"kb")
        return out

    def fake_concat(segments, out):
        out.write_bytes(b"concat")
        return out

    def fake_mux(video, voice_mp3s, out, **_):
        out.write_bytes(b"mux")
        return out

    def fake_burn(video, srt, out, **_):
        out.write_bytes(b"burn")
        return out

    def fake_run(args, **_):
        # Simulate normalize-LTX ffmpeg call (writes the seg file path arg).
        # The last positional arg is the output mp4.
        out = Path(args[-1])
        out.write_bytes(b"ltx-norm")

    monkeypatch.setattr("src.ai_talent.assemble.video_compositor.ken_burns",
                        fake_ken_burns)
    monkeypatch.setattr("src.ai_talent.assemble.video_compositor.concat_segments",
                        fake_concat)
    monkeypatch.setattr("src.ai_talent.assemble.video_compositor.mux_audio",
                        fake_mux)
    monkeypatch.setattr("src.ai_talent.assemble.video_compositor.burn_subtitles",
                        fake_burn)
    monkeypatch.setattr("src.ai_talent.assemble.video_compositor._run",
                        fake_run)

    # --- bitrate_fitter.fit_to_size: copy + return ---
    def fake_fit(src, out, target_mb=18.0, audio_kbps=96):
        out.write_bytes(b"final-mp4-data")
        return out

    monkeypatch.setattr("src.ai_talent.assemble.bitrate_fitter.fit_to_size",
                        fake_fit)

    # --- LTX API: track call count via list ---
    def fake_ltx_generate(*, prompt, duration_sec=5, model=None,
                          resolution=None, fps=24, image_path=None,
                          generate_audio=False, **_):
        ltx_called.append({"prompt": prompt, "duration": duration_sec,
                           "image_path": image_path})
        return b"\x00\x00\x00\x18ftypmp4"

    monkeypatch.setattr("src.ai_talent.assemble._ltx_api.generate",
                        fake_ltx_generate)
    monkeypatch.setattr("src.ai_talent.assemble._ltx_api.estimate_cost",
                        lambda *a, **k: 0.40)

    # --- spend tracker preflight + record are no-ops ---
    monkeypatch.setattr("src.ai_talent.assemble.preflight_check",
                        lambda *a, **k: None)
    monkeypatch.setattr("src.ai_talent.assemble.record_provider_spend",
                        lambda *a, **k: None)

    # --- ffprobe subprocess for final result dict ---
    real_run = __import__("subprocess").run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if cmd[:1] == ["ffprobe"]:
            class _R:
                stdout = json.dumps({"format": {"duration": "30.0",
                                                "size": "1000000"}})
                returncode = 0
            return _R()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    return {}


def test_assemble_density_C_skips_ltx_stage(tmp_path, monkeypatch,
                                              mock_brief_and_yaml):
    brief, char_yaml = mock_brief_and_yaml
    ltx_called: list = []
    _patch_stages(monkeypatch, tmp_path, ltx_called)
    final_dir = tmp_path / "final"
    result = assemble.assemble(
        brief_path=brief, slug="my-slug", ltx_density="C",
        char_yaml_path=char_yaml,
        spend_file=tmp_path / "spend.json",
        final_dir=final_dir, run_preflight=False,
    )
    assert ltx_called == []
    assert result["ltx_density"] == "C"
    assert (final_dir / "my-slug.mp4").exists()


def test_assemble_density_B_calls_ltx_once(tmp_path, monkeypatch,
                                             mock_brief_and_yaml):
    brief, char_yaml = mock_brief_and_yaml
    ltx_called: list = []
    _patch_stages(monkeypatch, tmp_path, ltx_called)
    assemble.assemble(
        brief_path=brief, slug="my-slug", ltx_density="B",
        char_yaml_path=char_yaml,
        spend_file=tmp_path / "spend.json",
        final_dir=tmp_path / "final", run_preflight=False,
    )
    assert len(ltx_called) == 1
    # The hero beat is b2 → "OHWX_FORTONA scene 2"
    assert "scene 2" in ltx_called[0]["prompt"]


def test_assemble_density_A_calls_ltx_per_beat(tmp_path, monkeypatch,
                                                 mock_brief_and_yaml):
    brief, char_yaml = mock_brief_and_yaml
    ltx_called: list = []
    _patch_stages(monkeypatch, tmp_path, ltx_called)
    assemble.assemble(
        brief_path=brief, slug="my-slug", ltx_density="A",
        char_yaml_path=char_yaml,
        spend_file=tmp_path / "spend.json",
        final_dir=tmp_path / "final", run_preflight=False,
    )
    # 4 beats in the fake script
    assert len(ltx_called) == 4


def test_assemble_writes_final_to_final_dir(tmp_path, monkeypatch,
                                              mock_brief_and_yaml):
    brief, char_yaml = mock_brief_and_yaml
    ltx_called: list = []
    _patch_stages(monkeypatch, tmp_path, ltx_called)
    final_dir = tmp_path / "assets" / "video" / "test"
    result = assemble.assemble(
        brief_path=brief, slug="test-centry-smoke", ltx_density="C",
        char_yaml_path=char_yaml,
        spend_file=tmp_path / "spend.json",
        final_dir=final_dir, run_preflight=False,
    )
    assert result["out_path"].endswith("assets/video/test/test-centry-smoke.mp4")
    assert "queue" not in result["out_path"]


def test_assemble_returns_summary_dict(tmp_path, monkeypatch,
                                         mock_brief_and_yaml):
    brief, char_yaml = mock_brief_and_yaml
    ltx_called: list = []
    _patch_stages(monkeypatch, tmp_path, ltx_called)
    result = assemble.assemble(
        brief_path=brief, slug="x-y", ltx_density="C",
        char_yaml_path=char_yaml,
        spend_file=tmp_path / "spend.json",
        final_dir=tmp_path / "f", run_preflight=False,
    )
    assert set(result.keys()) >= {"slug", "out_path", "size_mb",
                                   "duration_sec", "ltx_density"}
    assert result["slug"] == "x-y"
    assert isinstance(result["size_mb"], (int, float))
    assert isinstance(result["duration_sec"], (int, float))


# -- CLI ----------------------------------------------------------------------

def test_cli_help_does_not_crash(capsys):
    with pytest.raises(SystemExit) as ex:
        assemble._cli(["--help"])
    assert ex.value.code == 0


def test_cli_rejects_bad_slug(monkeypatch, capsys, tmp_path):
    brief = tmp_path / "x.md"
    brief.write_text("---\n---\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        assemble._cli(["--brief", str(brief), "--slug", "BAD"])


def test_cli_returns_1_on_assemble_error(monkeypatch, tmp_path, capsys):
    """When assemble() raises AssembleError, CLI prints to stderr and returns 1."""
    brief = tmp_path / "x.md"
    brief.write_text("---\n---\n", encoding="utf-8")

    def _raise(**kwargs):
        raise assemble.AssembleError("mocked failure")

    monkeypatch.setattr(assemble, "assemble", _raise)
    rc = assemble._cli(["--brief", str(brief), "--slug", "ok-slug",
                          "--no-preflight"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "mocked failure" in err
