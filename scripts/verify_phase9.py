"""Phase 9 closure check — exits 0 only when every CHAR-03/04/05/06 gate is GREEN.

Single-command Phase 9 acceptance gate.

Usage:
    python scripts/verify_phase9.py
    python scripts/verify_phase9.py --strict   # also runs pytest

Gates:
- CHAR-03: dataset/v1 — 25-35 paired jpg/txt + MANIFEST.json + trigger-word-only captions
- CHAR-04: character.yaml.lora.status=ready + trigger lock + model+sha + steps cap
- CHAR-05: smoke/v1 — 5 named PNGs + collage_1x5.png + anchor_prompts.txt
- CHAR-06: anchor/v1/anchor.json — Facenet512, ≥5 embeddings × 512 dims, threshold=0.85
- BOOT-01: .metrics/api_spend.json — replicate month-to-date ≤ $6 cap
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Each gate appends (req_id, message) — PASS messages start with "PASS", FAILs with "FAIL".
GATES: list[tuple[str, str]] = []


def _yaml_load(path: Path) -> dict:
    """Lightweight yaml loader. Prefers PyYAML if available; falls back to simple parser
    that covers our character.yaml structure (string scalars + nested mappings)."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    # Minimal fallback: parse `key: value` lines, supporting top-level + 1-2 levels of nesting
    # via indent. Sufficient for character.yaml.lora.* and lora.training_metadata.*.
    out: dict = {}
    stack: list[tuple[int, dict]] = [(-1, out)]
    line_re = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = line_re.match(raw)
        if not m:
            continue
        indent = len(m.group(1))
        key = m.group(2)
        val = m.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val == "" or val is None:
            new: dict = {}
            parent[key] = new
            stack.append((indent, new))
        else:
            # Strip surrounding quotes
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            # Try int
            if re.fullmatch(r"-?\d+", val):
                parent[key] = int(val)
            elif re.fullmatch(r"-?\d+\.\d+", val):
                parent[key] = float(val)
            elif val in ("true", "false"):
                parent[key] = val == "true"
            elif val in ("null", "~"):
                parent[key] = None
            else:
                parent[key] = val
    return out


def fail(req: str, msg: str) -> None:
    GATES.append((req, f"FAIL — {msg}"))


def passed(req: str, msg: str) -> None:
    GATES.append((req, f"PASS — {msg}"))


def check_char_03() -> None:
    d = ROOT / "ai_talent/dataset/v1"
    if not d.exists():
        return fail("CHAR-03", f"dataset dir missing: {d}")
    jpgs = sorted(d.glob("*.jpg"))
    txts = sorted(d.glob("*.txt"))
    if len(jpgs) < 25 or len(jpgs) > 35:
        return fail("CHAR-03", f"dataset jpg count {len(jpgs)} not in [25,35]")
    if len(txts) != len(jpgs):
        return fail("CHAR-03", f"caption count {len(txts)} != jpg count {len(jpgs)}")
    for txt in txts:
        content = txt.read_text(encoding="utf-8").strip()
        if not content.startswith("OHWX_FORTONA,"):
            return fail("CHAR-03", f"caption {txt.name} missing trigger word")
        lower = content.lower()
        # Captions must NOT re-encode looks/identity — only describe scene/pose/lighting.
        for forbidden in ("brunette", "blue eyes", "26-year", "russian", "brown hair"):
            if forbidden in lower:
                return fail("CHAR-03", f"caption {txt.name} leaks identity: '{forbidden}'")
    if not (d / "MANIFEST.json").exists():
        return fail("CHAR-03", "MANIFEST.json missing")
    # LFS check — informational, не блокирующее
    ga = ROOT / ".gitattributes"
    if ga.exists() and "ai_talent/dataset" in ga.read_text(encoding="utf-8"):
        passed(
            "CHAR-03",
            f"{len(jpgs)} dataset frames + MANIFEST + LFS rule; captions clean",
        )
    else:
        passed(
            "CHAR-03",
            f"{len(jpgs)} dataset frames + MANIFEST; captions clean (LFS rule unverified)",
        )


def check_char_04() -> None:
    cy = ROOT / "ai_talent/character.yaml"
    if not cy.exists():
        return fail("CHAR-04", "character.yaml missing")
    d = _yaml_load(cy)
    lora = d.get("lora") or {}
    if lora.get("status") != "ready":
        return fail("CHAR-04", f"lora.status={lora.get('status')!r} != 'ready'")
    if lora.get("trigger_word") != "OHWX_FORTONA":
        return fail("CHAR-04", f"trigger_word={lora.get('trigger_word')!r} drifted")
    model = lora.get("model")
    sha = lora.get("version_sha256")
    if not model:
        return fail("CHAR-04", "lora.model missing")
    if not sha or not re.fullmatch(r"[a-f0-9]{64}", str(sha)):
        return fail("CHAR-04", f"lora.version_sha256={sha!r} not a sha256")
    dsize = lora.get("training_dataset_size")
    if not isinstance(dsize, int) or dsize < 25 or dsize > 35:
        return fail("CHAR-04", f"training_dataset_size={dsize} not in [25,35]")
    meta = lora.get("training_metadata") or {}
    steps = meta.get("steps")
    if not isinstance(steps, int) or steps <= 0 or steps > 1500:
        return fail("CHAR-04", f"steps={steps} not in (0, 1500]")
    passed(
        "CHAR-04",
        f"lora ready: {model}:{str(sha)[:12]}… (steps={steps}, ds={dsize})",
    )


def check_char_05() -> None:
    s = ROOT / "ai_talent/smoke/v1"
    if not s.exists():
        return fail("CHAR-05", f"smoke dir missing: {s}")
    names = ["01_closeup", "02_three_quarter", "03_fullbody", "04_profile", "05_emotion"]
    for n in names:
        if not (s / f"{n}.png").exists():
            return fail("CHAR-05", f"missing {n}.png")
    if not (s / "anchor_prompts.txt").exists():
        return fail("CHAR-05", "anchor_prompts.txt missing")
    if not (s / "collage_1x5.png").exists():
        return fail("CHAR-05", "collage_1x5.png missing")
    passed("CHAR-05", "5 smoke PNGs + collage_1x5.png + anchor_prompts.txt present")


def check_char_06() -> None:
    a = ROOT / "ai_talent/anchor/v1/anchor.json"
    if not a.exists():
        return fail("CHAR-06", "anchor.json missing")
    try:
        d = json.loads(a.read_text(encoding="utf-8"))
    except Exception as e:
        return fail("CHAR-06", f"anchor.json not valid JSON: {e}")
    if d.get("model") != "Facenet512":
        return fail("CHAR-06", f"anchor.model={d.get('model')!r} != 'Facenet512'")
    if d.get("threshold") != 0.85:
        return fail("CHAR-06", f"anchor.threshold={d.get('threshold')} != 0.85")
    embs = d.get("embeddings") or {}
    if len(embs) < 5:
        return fail("CHAR-06", f"embeddings count {len(embs)} < 5")
    for k, v in embs.items():
        if not isinstance(v, list) or len(v) != 512:
            return fail(
                "CHAR-06",
                f"embedding {k} has {len(v) if isinstance(v, list) else '?'} dims, not 512",
            )
    # Sanity: anchor must reference the same LoRA version as character.yaml
    cy = ROOT / "ai_talent/character.yaml"
    if cy.exists():
        cdoc = _yaml_load(cy)
        char_sha = (cdoc.get("lora") or {}).get("version_sha256")
        anch_sha = (d.get("character_yaml_lora") or {}).get("version_sha256")
        if char_sha and anch_sha and char_sha != anch_sha:
            return fail(
                "CHAR-06",
                f"anchor lora sha {str(anch_sha)[:12]}… != character.yaml sha {str(char_sha)[:12]}…",
            )
    passed(
        "CHAR-06",
        f"anchor valid (Facenet512, {len(embs)}×512, threshold=0.85, LoRA SHA aligned)",
    )


def check_spend_cap() -> None:
    sp = ROOT / ".metrics/api_spend.json"
    if not sp.exists():
        return fail("BOOT-01", "spend file missing")
    try:
        d = json.loads(sp.read_text(encoding="utf-8"))
    except Exception as e:
        return fail("BOOT-01", f"spend file not valid JSON: {e}")
    # Find current month bucket (highest by_provider.replicate.usd)
    replicate_mtd = 0.0
    for key, val in d.items():
        if not isinstance(val, dict):
            continue
        rep = (val.get("by_provider") or {}).get("replicate") or {}
        usd = rep.get("usd")
        if isinstance(usd, (int, float)):
            replicate_mtd = max(replicate_mtd, float(usd))
    if replicate_mtd > 6.0:
        return fail("BOOT-01", f"replicate mtd ${replicate_mtd:.2f} exceeds $6 cap")
    passed("BOOT-01", f"replicate mtd ${replicate_mtd:.2f} within $6 cap")


def check_pytest() -> int:
    """Return 1 on fail, 0 on pass. Runs `pytest -q` if pytest is available."""
    pytest_bin = ROOT / ".venv/bin/pytest"
    cmd = [str(pytest_bin)] if pytest_bin.exists() else ["pytest"]
    cmd += ["-q", "--no-header", "tests/"]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    except FileNotFoundError:
        fail("PYTEST", "pytest not installed")
        return 1
    tail = (r.stdout or "").splitlines()[-5:]
    m = re.search(r"(\d+)\s+passed", r.stdout or "")
    passed_count = int(m.group(1)) if m else 0
    if r.returncode == 0 and passed_count >= 354:
        passed("PYTEST", f"{passed_count} passed (≥354 baseline)")
        return 0
    fail(
        "PYTEST",
        f"rc={r.returncode}, passed={passed_count} (need ≥354). tail:\n  "
        + "\n  ".join(tail),
    )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 9 acceptance gate")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="also run pytest -q (≥354 passing required)",
    )
    args = ap.parse_args()

    check_char_03()
    check_char_04()
    check_char_05()
    check_char_06()
    check_spend_cap()
    if args.strict:
        check_pytest()

    any_failed = False
    print("=== Phase 9 acceptance gate ===")
    print(f"Repo: {ROOT}")
    print()
    for req, msg in GATES:
        mark = "[x]" if msg.startswith("PASS") else "[ ]"
        print(f"  {mark} {req}: {msg}")
        if msg.startswith("FAIL"):
            any_failed = True

    print()
    if any_failed:
        print("Phase 9 ACCEPTANCE: RED — see fails above.")
        return 1
    print("Phase 9 ACCEPTANCE: GREEN — ready to close.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
