"""Stage hash-and-skip cache for the AI-talent pipeline.

Phase 11 invariant: each pipeline stage computes a SHA256 of its inputs,
writes ``.sha256`` atomically AFTER ``run_fn`` returns successfully, and skips
work on the next invocation if hash + output marker both still match.

Critical anti-pattern (Pitfall 1 in 11-RESEARCH.md): committing ``.sha256``
BEFORE ``run_fn`` completes creates stale partial output that surfaces N
stages later as an opaque error. This module asserts the invariant in one
tested place: ``commit`` is the very last act inside ``run_stage``, gated on
a clean return from ``run_fn``.

Public API:

* ``Stage(slug, stage_num, name, cache_root=...)``
    - ``.dir`` -> ``Path`` (``<cache_root>/<slug>/<NN>-<name>/``)
    - ``.sha_file`` -> ``Path`` (``<dir>/.sha256``)
    - ``.hit(current_hash, output_marker) -> bool``
    - ``.commit(current_hash) -> None``   # atomic — tempfile + os.replace
    - ``.invalidate() -> None``
* ``run_stage(*, slug, stage_num, name, inputs_for_hash, output_marker,
              run_fn, cache_root=None, force=False) -> Path``
* ``_sha256(content: bytes | str) -> str``
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable, Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_ROOT: Final[Path] = _REPO_ROOT / ".cache"


def _sha256(content: bytes | str) -> str:
    """Deterministic SHA256 hex digest. UTF-8 encodes strings."""
    h = hashlib.sha256()
    h.update(content.encode("utf-8") if isinstance(content, str) else content)
    return h.hexdigest()


class Stage:
    """One stage of the pipeline cache: ``<cache_root>/<slug>/<NN>-<name>/``.

    Lifecycle (enforced by :func:`run_stage`):

    1. Caller computes ``current_hash = _sha256(<inputs>)``.
    2. If ``hit(current_hash, output_marker)`` -> return cached output path.
    3. Else: ``run_fn(stage.dir)`` produces the ``output_marker`` file.
    4. On clean return: ``commit(current_hash)`` writes ``.sha256`` atomically.
    5. On exception: ``commit`` is NOT called -> next run rebuilds.
    """

    def __init__(
        self,
        slug: str,
        stage_num: int,
        name: str,
        cache_root: Path | None = None,
    ):
        root = cache_root if cache_root is not None else DEFAULT_CACHE_ROOT
        self.dir: Path = root / slug / f"{stage_num:02d}-{name}"
        self.sha_file: Path = self.dir / ".sha256"

    def hit(self, current_hash: str, output_marker: str) -> bool:
        """Return ``True`` iff ``.sha256`` exists AND matches AND output marker exists."""
        if not self.sha_file.exists():
            return False
        try:
            stored = self.sha_file.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if stored != current_hash:
            return False
        return (self.dir / output_marker).exists()

    def commit(self, current_hash: str) -> None:
        """POSIX-atomic write of ``.sha256``.

        MUST be called only after ``run_fn`` returns without exception. Writes
        to a temp file in the stage dir, then ``os.replace`` onto ``.sha256``
        (same filesystem -> atomic rename).
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".sha256.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(current_hash + "\n")
            os.replace(tmp, self.sha_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def invalidate(self) -> None:
        """Delete ``.sha256`` so the next :meth:`hit` returns ``False``.

        Used by ``--force-stage`` / ``force=True`` paths in callers.
        Idempotent: silent if the file is already absent.
        """
        try:
            self.sha_file.unlink()
        except FileNotFoundError:
            pass


def run_stage(
    *,
    slug: str,
    stage_num: int,
    name: str,
    inputs_for_hash: bytes | str,
    output_marker: str,
    run_fn: Callable[[Path], None],
    cache_root: Path | None = None,
    force: bool = False,
) -> Path:
    """Orchestrate one stage: hash -> hit-check -> run_fn -> commit.

    Returns the path to the produced output marker file
    (``<stage.dir>/<output_marker>``).

    Raises whatever ``run_fn`` raises (without committing ``.sha256`` —
    so the next call rebuilds).
    """
    stage = Stage(slug, stage_num, name, cache_root=cache_root)
    stage.dir.mkdir(parents=True, exist_ok=True)

    current_hash = _sha256(inputs_for_hash)

    if force:
        stage.invalidate()
    elif stage.hit(current_hash, output_marker):
        return stage.dir / output_marker

    # Run the stage. If run_fn raises, .sha256 is NOT touched — next call rebuilds.
    run_fn(stage.dir)

    # Only after clean return — commit.
    stage.commit(current_hash)
    return stage.dir / output_marker


__all__ = ["Stage", "_sha256", "run_stage", "DEFAULT_CACHE_ROOT"]
