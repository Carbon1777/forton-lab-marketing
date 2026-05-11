"""Tests for pipeline_cache.Stage / run_stage idempotency primitives.

Phase 11-02 — hash-and-skip foundation for all 7 pipeline stages.
Critical invariant under test: commit only AFTER run_fn returns clean
(prevents stale partial output — Pitfall 1 in 11-RESEARCH.md).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ai_talent.pipeline_cache import (
    Stage,
    _sha256,
    run_stage,
)


def test_sha256_deterministic():
    assert _sha256("abc") == _sha256("abc")
    assert _sha256("abc") != _sha256("abd")


def test_sha256_accepts_bytes_and_str():
    assert _sha256(b"x") == _sha256("x")
    # multi-byte UTF-8 should hash identically as str and as encoded bytes
    assert _sha256("привет") == _sha256("привет".encode("utf-8"))


def test_stage_miss_when_no_sha_file(tmp_path: Path):
    s = Stage(slug="test-slug", stage_num=1, name="script",
              cache_root=tmp_path)
    assert s.hit("anyhash", "out.json") is False


def test_stage_miss_when_hash_differs(tmp_path: Path):
    s = Stage(slug="test-slug", stage_num=1, name="script",
              cache_root=tmp_path)
    s.dir.mkdir(parents=True, exist_ok=True)
    (s.dir / "out.json").write_text("{}")
    s.commit("hash_A")
    assert s.hit("hash_B", "out.json") is False


def test_stage_miss_when_output_marker_missing(tmp_path: Path):
    s = Stage(slug="test-slug", stage_num=1, name="script",
              cache_root=tmp_path)
    s.dir.mkdir(parents=True, exist_ok=True)
    s.commit("hash_A")
    # output marker not written
    assert s.hit("hash_A", "out.json") is False


def test_stage_hit_when_hash_and_output_match(tmp_path: Path):
    s = Stage(slug="test-slug", stage_num=1, name="script",
              cache_root=tmp_path)
    s.dir.mkdir(parents=True, exist_ok=True)
    (s.dir / "out.json").write_text("{}")
    s.commit("hash_A")
    assert s.hit("hash_A", "out.json") is True


def test_stage_commit_atomic(tmp_path: Path):
    s = Stage(slug="test-slug", stage_num=1, name="script",
              cache_root=tmp_path)
    s.dir.mkdir(parents=True, exist_ok=True)
    s.commit("hash_A")
    # Verify .sha256 exists and contains the hash
    sha_file = s.dir / ".sha256"
    assert sha_file.exists()
    assert sha_file.read_text().strip() == "hash_A"
    # Verify no leftover .tmp files
    tmps = list(s.dir.glob("*.tmp"))
    assert tmps == [], f"leftover tmpfile: {tmps}"


def test_stage_invalidate_removes_sha_file(tmp_path: Path):
    s = Stage(slug="test-slug", stage_num=1, name="script",
              cache_root=tmp_path)
    s.dir.mkdir(parents=True, exist_ok=True)
    (s.dir / "out.json").write_text("{}")
    s.commit("hash_A")
    assert (s.dir / ".sha256").exists()
    s.invalidate()
    assert not (s.dir / ".sha256").exists()
    assert s.hit("hash_A", "out.json") is False


def test_run_stage_skip_on_cache_hit(tmp_path: Path):
    calls = []

    def run_fn(stage_dir: Path) -> None:
        calls.append(stage_dir)
        (stage_dir / "out.json").write_text('{"ok": true}')

    # First call — should execute
    run_stage(
        slug="test-slug",
        stage_num=1,
        name="script",
        inputs_for_hash="input_x",
        output_marker="out.json",
        run_fn=run_fn,
        cache_root=tmp_path,
    )
    assert len(calls) == 1

    # Second call — should skip (cache hit)
    run_stage(
        slug="test-slug",
        stage_num=1,
        name="script",
        inputs_for_hash="input_x",
        output_marker="out.json",
        run_fn=run_fn,
        cache_root=tmp_path,
    )
    assert len(calls) == 1, "run_fn called twice despite cache hit"


def test_run_stage_crash_midway_invalidates_cache(tmp_path: Path):
    """The critical invariant: if run_fn raises, .sha256 must NOT be written,
    so the next invocation rebuilds (no stale partial output)."""
    calls = []

    def crashing_run_fn(stage_dir: Path) -> None:
        calls.append(stage_dir)
        (stage_dir / "out.json").write_text('{"partial": "garbage"}')
        raise RuntimeError("network glitch mid-stage")

    # First call — crashes
    with pytest.raises(RuntimeError, match="network glitch"):
        run_stage(
            slug="test-slug",
            stage_num=1,
            name="script",
            inputs_for_hash="input_x",
            output_marker="out.json",
            run_fn=crashing_run_fn,
            cache_root=tmp_path,
        )
    # .sha256 must NOT exist
    sha = tmp_path / "test-slug" / "01-script" / ".sha256"
    assert not sha.exists(), "premature .sha256 — would cause stale cache"

    # Second call with healthy run_fn — must execute (NOT cached)
    def healthy_run_fn(stage_dir: Path) -> None:
        calls.append(stage_dir)
        (stage_dir / "out.json").write_text('{"healthy": true}')

    run_stage(
        slug="test-slug",
        stage_num=1,
        name="script",
        inputs_for_hash="input_x",
        output_marker="out.json",
        run_fn=healthy_run_fn,
        cache_root=tmp_path,
    )
    assert len(calls) == 2, "healthy run_fn must be called after crash"


def test_run_stage_force_invalidates(tmp_path: Path):
    calls = []

    def run_fn(stage_dir: Path) -> None:
        calls.append(stage_dir)
        (stage_dir / "out.json").write_text("{}")

    run_stage(
        slug="t",
        stage_num=1,
        name="script",
        inputs_for_hash="x",
        output_marker="out.json",
        run_fn=run_fn,
        cache_root=tmp_path,
    )
    run_stage(
        slug="t",
        stage_num=1,
        name="script",
        inputs_for_hash="x",
        output_marker="out.json",
        run_fn=run_fn,
        cache_root=tmp_path,
        force=True,
    )
    assert len(calls) == 2, "force=True must re-run despite cache hit"
