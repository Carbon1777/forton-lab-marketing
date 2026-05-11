"""Phase 11 pre-flight smoke test — 5 health checks before any production assemble run.

Defends against runaway burn (P15). Run via ``python -m src.ai_talent.preflight [--json]``.
Exit 0 = all GREEN; exit 1 = at least one RED.

Five checks:
    1. replicate       — REPLICATE_API_TOKEN + users.get_current()
    2. elevenlabs      — ELEVENLABS_API_KEY + paid tier
    3. ltx             — LTX_API_KEY env OR .env.ltx file fallback (ltxv_ prefix)
    4. spend_tracker   — file absent OR _schema_version>=3
    5. character_yaml  — lora.status=="ready" AND voice.status=="ready"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Callable, Final

import replicate  # used by check_replicate; tests monkeypatch this attribute
import yaml

from src.elevenlabs_tier import get_studio_tier, is_paid_tier
from src.spend_tracker_v2 import read_monthly_total

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_SPEND_FILE: Final[Path] = _REPO_ROOT / ".metrics" / "api_spend.json"
DEFAULT_CHARACTER_YAML: Final[Path] = _REPO_ROOT / "ai_talent" / "character.yaml"
DEFAULT_ENV_LTX: Final[Path] = _REPO_ROOT.parent / ".env.ltx"


def check_replicate() -> tuple[bool, str]:
    """Verify REPLICATE_API_TOKEN env + token actually authenticates."""
    if not os.environ.get("REPLICATE_API_TOKEN"):
        return False, "REPLICATE_API_TOKEN missing"
    try:
        client = replicate.Client(api_token=os.environ["REPLICATE_API_TOKEN"])
        user = client.users.get_current()
        username = getattr(user, "username", "?")
        return True, f"OK (user={username})"
    except Exception as e:
        return False, f"replicate auth failed: {e}"


def check_elevenlabs() -> tuple[bool, str]:
    """Verify ELEVENLABS_API_KEY + paid tier (BOOT-02)."""
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return False, "ELEVENLABS_API_KEY missing"
    tier = get_studio_tier()
    if not is_paid_tier(tier):
        return False, f"tier={tier!r} not paid (BOOT-02 violation)"
    return True, f"OK (tier={tier})"


def check_ltx() -> tuple[bool, str]:
    """LTX_API_KEY env OR .env.ltx file; both must have ltxv_ prefix."""
    env_key = os.environ.get("LTX_API_KEY", "").strip()
    if env_key:
        if env_key.startswith("ltxv_"):
            return True, "OK (env)"
        return False, "LTX_API_KEY missing 'ltxv_' prefix"
    if DEFAULT_ENV_LTX.exists():
        file_key = DEFAULT_ENV_LTX.read_text(encoding="utf-8").strip()
        if file_key.startswith("ltxv_"):
            return True, f"OK ({DEFAULT_ENV_LTX})"
        return False, f"key in {DEFAULT_ENV_LTX} missing 'ltxv_' prefix"
    return False, "LTX_API_KEY env not set and .env.ltx missing"


def check_spend_tracker(spend_file: Path = DEFAULT_SPEND_FILE) -> tuple[bool, str]:
    """File absent → clean slate (pass). Otherwise require _schema_version>=3."""
    if not spend_file.exists():
        return True, "no file yet — clean slate OK"
    try:
        data = json.loads(spend_file.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"spend tracker corrupt: {e}"
    schema = data.get("_schema_version", 0)
    if not isinstance(schema, int) or schema < 3:
        return False, f"schema_version={schema} <3 (run Phase 7 migrate)"
    try:
        month = dt.date.today().strftime("%Y-%m")
        month_total = read_monthly_total(spend_file, month)
    except Exception as e:
        return False, f"read_monthly_total failed: {e}"
    return True, f"OK (v{schema}, {month}=${month_total:.2f})"


def check_character_yaml(yaml_path: Path = DEFAULT_CHARACTER_YAML) -> tuple[bool, str]:
    """Assert character.yaml has lora.status==ready AND voice.status==ready."""
    if not yaml_path.exists():
        return False, f"character.yaml missing: {yaml_path}"
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return False, f"character.yaml unparseable: {e}"
    if not isinstance(data, dict):
        return False, f"character.yaml not a mapping (got {type(data).__name__})"
    lora = data.get("lora") or {}
    voice = data.get("voice") or {}
    if lora.get("status") != "ready":
        return False, f"lora.status={lora.get('status')!r} (run Phase 9)"
    if voice.get("status") != "ready":
        return False, f"voice.status={voice.get('status')!r} (run Phase 10)"
    return True, "OK (lora + voice ready)"


Check = Callable[[], tuple[bool, str]]
CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("replicate", check_replicate),
    ("elevenlabs", check_elevenlabs),
    ("ltx", check_ltx),
    ("spend_tracker", check_spend_tracker),
    ("character_yaml", check_character_yaml),
)


def run_checks() -> tuple[bool, list[dict]]:
    """Run all CHECKS; return (all_pass, [{check,pass,msg}, ...])."""
    results: list[dict] = []
    all_pass = True
    for name, fn in CHECKS:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"check raised: {e}"
        results.append({"check": name, "pass": ok, "msg": msg})
        all_pass = all_pass and ok
    return all_pass, results


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point. Returns 0 GREEN / 1 RED."""
    ap = argparse.ArgumentParser(description="Phase 11 pre-flight check (PIPE-04)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON result to stdout instead of human-readable text")
    args = ap.parse_args(argv)

    all_pass, results = run_checks()

    if args.json:
        print(json.dumps({"green": all_pass, "checks": results}, indent=2,
                         ensure_ascii=False))
    else:
        for r in results:
            mark = "OK  " if r["pass"] else "FAIL"
            print(f"  [{mark}] {r['check']}: {r['msg']}")
        print()
        print("GREEN" if all_pass else "RED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "check_replicate", "check_elevenlabs", "check_ltx",
    "check_spend_tracker", "check_character_yaml",
    "run_checks", "main",
    "CHECKS",
    "DEFAULT_SPEND_FILE", "DEFAULT_CHARACTER_YAML", "DEFAULT_ENV_LTX",
]
