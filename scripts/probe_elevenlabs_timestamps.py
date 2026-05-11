"""Probe: does elevenlabs SDK ≥2.46 expose `convert_with_timestamps`?

Resolves Open Question Q-ELEVEN-TS (see .planning/phases/11-pipeline-scaffolding/
11-RESEARCH.md §Open Questions) BEFORE Plan 05 writes `srt_builder.py` +
`voice_synth.py`. If the SDK exposes a timestamp-bearing TTS method, Plan 05
uses Option A (real character-level timestamps from ElevenLabs). Otherwise
Plan 05 falls back to Option C (punctuation-based SRT splitter).

Cost: $0 — introspection only, no TTS call. Probe inspects the SDK module
surface; no audio is generated.

Usage:
    python scripts/probe_elevenlabs_timestamps.py
    python scripts/probe_elevenlabs_timestamps.py --dry-run     # skip introspection
"""
from __future__ import annotations

import argparse
import sys


def _scan_module(mod_path: str) -> list[str]:
    """Import the module by dotted path; return public attr names containing 'timestamp'."""
    try:
        mod = __import__(mod_path, fromlist=["*"])
    except ImportError:
        return []
    return [m for m in dir(mod) if "timestamp" in m.lower() and not m.startswith("_")]


def _scan_class(cls) -> list[str]:
    return [m for m in dir(cls) if "timestamp" in m.lower() and not m.startswith("_")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip introspection (smoke test that the script is callable).")
    args = ap.parse_args(argv)

    if args.dry_run:
        print("[dry-run] would import elevenlabs and inspect text_to_speech surface")
        return 0

    try:
        import elevenlabs
    except ImportError as e:
        print(f"FAIL: elevenlabs not installed: {e}", file=sys.stderr)
        return 1
    sdk_version = getattr(elevenlabs, "__version__", "<unknown>")
    print(f"elevenlabs SDK version: {sdk_version}")

    # Scan the public TTS surfaces. SDK 2.46 layout is
    # `elevenlabs.text_to_speech.client` (module) + TextToSpeechClient (class).
    candidates_mod = [
        "elevenlabs.text_to_speech.client",
        "elevenlabs.text_to_speech",
    ]
    found_module: list[tuple[str, list[str]]] = []
    for path in candidates_mod:
        hits = _scan_module(path)
        if hits:
            found_module.append((path, hits))

    found_class: list[tuple[str, list[str]]] = []
    try:
        from elevenlabs.text_to_speech.client import TextToSpeechClient  # type: ignore
        hits = _scan_class(TextToSpeechClient)
        if hits:
            found_class.append(("TextToSpeechClient", hits))
    except ImportError:
        pass

    # Older 2.x SDK aliased the surface under elevenlabs.client.ElevenLabs.text_to_speech
    try:
        from elevenlabs.client import ElevenLabs  # type: ignore
        attrs = dir(ElevenLabs)
        if "text_to_speech" in attrs:
            print("ElevenLabs client exposes `text_to_speech` namespace (good).")
    except ImportError:
        pass

    print()
    if found_module or found_class:
        print("Timestamp-bearing surfaces found:")
        for path, hits in found_module:
            print(f"  module {path}: {hits}")
        for name, hits in found_class:
            print(f"  class {name}: {hits}")
        print()
        print("=== Q-ELEVEN-TS ANSWER: timestamps available in SDK ===")
        print("=== Plan 05 uses Option A (real character-level timestamps) ===")
    else:
        print("No `*timestamp*` symbols found in elevenlabs.text_to_speech surface.")
        print()
        print("=== Q-ELEVEN-TS ANSWER: NO convert_with_timestamps in SDK ===")
        print("=== Plan 05 uses Option C fallback (punctuation-based SRT) ===")

    print()
    print("=" * 60)
    print("Append to /Users/jcat/Documents/Brain/projects/forton-lab/decisions.md:")
    print("=" * 60)
    print(f"## 2026-05-11 — Q-ELEVEN-TS resolved (Phase 11 Wave 0)")
    print(f"Probe: scripts/probe_elevenlabs_timestamps.py (sub-repo).")
    print(f"SDK version: {sdk_version}.")
    print(f"Answer: <paste «Q-ELEVEN-TS ANSWER» line above>.")
    print(f"Impact on Plan 05: srt_builder uses Option A or Option C accordingly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
