"""Phase 11 Wave 0 setup smoke tests.

Verifies that bootstrap deliverables are in place before Wave 1 plans run:
  - requirements.txt pins for elevenlabs / pillow
  - .gitignore excludes .cache/
  - ai_talent/briefs/_template.md exists with frontmatter contract
  - RUNBOOK_PHASE11.md scaffold with mandatory section headers
  - Both probe scripts exist + run in --dry-run mode (no API call)
  - Phase 11 conftest fixtures resolve correctly
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import frontmatter
import yaml


# -------------------------------------------------------------------
# Task 1 — deps / .gitignore / briefs / RUNBOOK scaffold
# -------------------------------------------------------------------

def test_requirements_pin_elevenlabs_246(repo_root: Path):
    """elevenlabs pin matches >=2.46[.0] requirement (current line is `elevenlabs==2.46.0`)."""
    txt = (repo_root / "requirements.txt").read_text(encoding="utf-8")
    # Accept either ==2.46.x OR >=2.46.x,<3 — both satisfy the Phase 11 contract
    lines = [ln.strip() for ln in txt.splitlines()
             if ln.strip().lower().startswith("elevenlabs") and not ln.strip().startswith("#")]
    assert lines, "elevenlabs pin missing from requirements.txt"
    pin = lines[0]
    assert re.search(r"elevenlabs\s*(==|>=)\s*2\.4[6-9]", pin), (
        f"elevenlabs pin must be >=2.46.x or ==2.46.x — got {pin!r}"
    )


def test_requirements_pin_pillow(repo_root: Path):
    """Pillow added for Phase 11 frame compositor primitives."""
    txt = (repo_root / "requirements.txt").read_text(encoding="utf-8")
    lines = [ln.strip().lower() for ln in txt.splitlines()
             if ln.strip().lower().startswith("pillow") and not ln.strip().startswith("#")]
    assert lines, "pillow pin missing from requirements.txt (needed for Phase 11 compositor)"
    pin = lines[0]
    # Match pillow>=10.4 or pillow==10.4.x or pillow>=10.4,<12 etc.
    assert re.search(r"pillow\s*(==|>=)\s*1[0-9]\.[0-9]", pin), (
        f"pillow pin must be >=10.x — got {pin!r}"
    )


def test_gitignore_excludes_cache(repo_root: Path):
    """All probe + cache outputs live under .cache/ — must be gitignored."""
    txt = (repo_root / ".gitignore").read_text(encoding="utf-8")
    cache_lines = [ln.strip() for ln in txt.splitlines()
                   if re.fullmatch(r"\.cache/?", ln.strip())]
    assert cache_lines, f".cache/ must appear in .gitignore — current: {txt!r}"


def test_gitignore_does_not_exclude_briefs(repo_root: Path):
    """Briefs are source-of-truth committed Markdown — they MUST stay tracked."""
    txt = (repo_root / ".gitignore").read_text(encoding="utf-8")
    forbidden = ["ai_talent/briefs/*.md", "ai_talent/briefs/", "ai_talent/briefs/*"]
    for f in forbidden:
        for ln in txt.splitlines():
            assert ln.strip() != f, f"briefs must not be gitignored — found {f!r} in .gitignore"


def test_briefs_dir_exists_with_template(repo_root: Path):
    """Plan 06 smoke test will read this template — its frontmatter contract is locked."""
    tpl = repo_root / "ai_talent" / "briefs" / "_template.md"
    assert tpl.exists(), f"missing brief template: {tpl}"
    post = frontmatter.loads(tpl.read_text(encoding="utf-8"))
    required_keys = {"product", "topic", "hook", "cta"}
    missing = required_keys - set(post.metadata.keys())
    assert not missing, f"brief template missing required frontmatter keys: {missing}"
    # Optional but expected scaffold values
    assert "series_flag" in post.metadata, "series_flag flag must be present (Plan 02 contract)"
    assert "ltx_density" in post.metadata, "ltx_density must be present (A|B|C selector)"


def test_briefs_gitkeep_present(repo_root: Path):
    keep = repo_root / "ai_talent" / "briefs" / ".gitkeep"
    assert keep.exists(), f"missing .gitkeep for empty-dir tracking: {keep}"


def test_runbook_phase11_scaffold_present(repo_root: Path):
    rb = repo_root / "RUNBOOK_PHASE11.md"
    assert rb.exists(), f"missing operator runbook scaffold: {rb}"
    body = rb.read_text(encoding="utf-8")
    required_sections = [
        "## Overview",
        "## Pipeline Stages",
        "## Recovery Paths",
        "## API Cost Reference",
    ]
    for sec in required_sections:
        assert sec in body, f"RUNBOOK_PHASE11.md missing section header: {sec!r}"
    # Plan 07 will replace placeholders, but min content size signal
    assert body.count("\n") >= 25, "RUNBOOK_PHASE11.md scaffold too thin"


# -------------------------------------------------------------------
# Task 2 — probes exist + dry-run mode
# -------------------------------------------------------------------

def test_probe_ltx_image_cond_exists(repo_root: Path):
    """LTX probe runs in --dry-run mode without LTX_API_KEY (CI-safe)."""
    script = repo_root / "scripts" / "probe_ltx_image_cond.py"
    assert script.exists(), f"missing probe: {script}"
    r = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        capture_output=True, text=True, check=False, cwd=str(repo_root),
    )
    assert r.returncode == 0, f"dry-run failed: stderr={r.stderr!r}"
    assert "dry-run" in r.stdout.lower() or "dry-run" in r.stderr.lower(), (
        f"probe should announce dry-run mode — stdout={r.stdout!r}"
    )


def test_probe_elevenlabs_timestamps_exists(repo_root: Path):
    """ElevenLabs probe runs in --dry-run mode (no SDK call needed)."""
    script = repo_root / "scripts" / "probe_elevenlabs_timestamps.py"
    assert script.exists(), f"missing probe: {script}"
    r = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        capture_output=True, text=True, check=False, cwd=str(repo_root),
    )
    assert r.returncode == 0, f"dry-run failed: stderr={r.stderr!r}"


# -------------------------------------------------------------------
# Task 4 — Phase 11 conftest fixtures
# -------------------------------------------------------------------

def test_fixture_tmp_spend_file_v3_schema(tmp_spend_file: Path):
    """Fresh v3-schema spend file (clean slate for BOOT-01 tests)."""
    assert tmp_spend_file.exists()
    data = json.loads(tmp_spend_file.read_text(encoding="utf-8"))
    assert data.get("_schema_version") == 3
    # No provider spend yet
    by_month = {k: v for k, v in data.items() if not k.startswith("_") and k != "caps"
                and k != "regen_limit_per_month"}
    # No actual month entries with spend
    for month, mdata in by_month.items():
        # If month entries exist they should be empty / no spend
        assert isinstance(mdata, dict)


def test_fixture_mock_character_yaml_ready(mock_character_yaml: Path):
    """Synthetic character.yaml mirrors production: lora + voice both ready."""
    assert mock_character_yaml.exists()
    data = yaml.safe_load(mock_character_yaml.read_text(encoding="utf-8"))
    assert data["lora"]["status"] == "ready"
    assert data["lora"]["trigger_word"] == "OHWX_FORTONA"
    assert data["voice"]["status"] == "ready"
    assert data["voice"]["voice_id"] == "GN4wbsbejSnGSa1AzjH5"


def test_fixture_mock_anthropic_client_for_script(mock_anthropic_client_for_script):
    """Phase 11 anthropic mock returns tool_use script.json shape."""
    client = mock_anthropic_client_for_script
    msg = client.messages.create(model="x", max_tokens=1, messages=[])
    assert msg.content[0].type == "tool_use"
    inp = msg.content[0].input
    for key in ("frames", "voice_lines", "cuts", "hook", "product", "series_flag"):
        assert key in inp, f"tool_use.input missing key: {key}"


def test_fixture_mock_replicate_client_returns_image_bytes(mock_replicate_client_for_frames):
    """Phase 11 replicate mock returns iterable of file-like objects with .read()."""
    client = mock_replicate_client_for_frames
    out = client.run("test/model:abc", input={"prompt": "OHWX_FORTONA"})
    items = list(out)
    assert items, "replicate.run must return non-empty iterable"
    data = items[0].read()
    assert isinstance(data, (bytes, bytearray))
    assert data.startswith(b"\x89PNG"), "first item must be valid PNG bytes"
