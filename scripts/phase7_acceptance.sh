#!/usr/bin/env bash
# Phase 7 acceptance gate (Bootstrap & Financial Gates).
# Run locally (not in CI — нужен access к Brain).
# Exit 0 = phase 7 готов к закрытию.
# Exit non-zero = что-то не сделано / упало.

set -u  # not -e — хотим прогнать ВСЕ проверки и собрать summary

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRAIN_DECISIONS="/Users/jcat/Documents/Brain/projects/forton-lab/decisions.md"
FAILS=0
PASSES=0

# Resolve python interpreter. We `cd "$REPO_ROOT"` before the python checks below,
# so a relative path to .venv works even when $REPO_ROOT contains spaces (the absolute
# path breaks `eval` quoting in check() — see #issue around Forton Lab spaces in path).
cd "$REPO_ROOT"
if [ -x ".venv/bin/python" ]; then
    PY="./.venv/bin/python"
    PYTEST="./.venv/bin/pytest"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
    PYTEST="pytest"
else
    PY="python"
    PYTEST="pytest"
fi

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        echo "  PASS  $name"
        PASSES=$((PASSES + 1))
    else
        echo "  FAIL  $name"
        echo "        cmd: $cmd"
        FAILS=$((FAILS + 1))
    fi
}

echo "=== Phase 7 acceptance gate ==="
echo "Repo: $REPO_ROOT"
echo

# --- BOOT-04: Brain decisions verification ---
echo "[BOOT-04] Brain decisions check"
if [ -f "$BRAIN_DECISIONS" ]; then
    check "Brain decisions file exists"      "test -f '$BRAIN_DECISIONS'"
    check "Brain decisions has 2026-05-10"   "grep -q '2026-05-10' '$BRAIN_DECISIONS'"
    check "Brain decisions has AI-talent"    "grep -q -i 'AI-talent' '$BRAIN_DECISIONS'"
    check "Brain decisions has character"    "grep -q -E -i 'character|типаж|персонаж' '$BRAIN_DECISIONS'"
else
    echo "  SKIP  Brain decisions file not found at $BRAIN_DECISIONS (running in CI?)"
    echo "        BOOT-04 verification requires local Brain access."
    FAILS=$((FAILS + 1))
fi
echo

# --- BOOT-01, BOOT-05: spend_tracker v3 imports ---
echo "[BOOT-01, BOOT-05] spend_tracker v3 imports"
cd "$REPO_ROOT"
check "record_provider_spend importable" \
    "PYTHONPATH=. $PY -c 'from src.spend_tracker_v2 import record_provider_spend, preflight_check, DailyCapExceededError, MonthlyAbortError, ProviderMonthlyCapExceededError'"
check "DEFAULT_DAILY_CAP_USD == 3.0" \
    "PYTHONPATH=. $PY -c 'from src.spend_tracker_v2 import DEFAULT_DAILY_CAP_USD; assert DEFAULT_DAILY_CAP_USD == 3.0'"
check "DEFAULT_MONTHLY_ABORT_USD == 15.0" \
    "PYTHONPATH=. $PY -c 'from src.spend_tracker_v2 import DEFAULT_MONTHLY_ABORT_USD; assert DEFAULT_MONTHLY_ABORT_USD == 15.0'"
check "v2 readers preserved (read_regen_count)" \
    "PYTHONPATH=. $PY -c 'from src.spend_tracker_v2 import read_regen_count, read_regen_limit, DEFAULT_REGEN_LIMIT'"
echo

# --- BOOT-02: elevenlabs_tier ---
echo "[BOOT-02] elevenlabs_tier module"
check "elevenlabs_tier importable" \
    "PYTHONPATH=. $PY -c 'from src.elevenlabs_tier import get_studio_tier, is_paid_tier, require_paid_tier, PAID_TIERS, TierMissingError'"
check "is_paid_tier(\"starter\") is True" \
    "PYTHONPATH=. $PY -c 'from src.elevenlabs_tier import is_paid_tier; assert is_paid_tier(\"starter\")'"
check "is_paid_tier(\"free\") is False" \
    "PYTHONPATH=. $PY -c 'from src.elevenlabs_tier import is_paid_tier; assert not is_paid_tier(\"free\")'"
echo

# --- BOOT-03: fonts presence + OFL ---
echo "[BOOT-03] RU-fallback fonts"
check "Cormorant Garamond Regular .ttf"    "test -f assets/fonts/cormorant-garamond/CormorantGaramond-Regular.ttf"
check "Cormorant Garamond Bold .ttf"       "test -f assets/fonts/cormorant-garamond/CormorantGaramond-Bold.ttf"
check "Cormorant Garamond OFL.txt"         "test -f assets/fonts/cormorant-garamond/OFL.txt"
check "Marck Script Regular .ttf"          "test -f assets/fonts/marck-script/MarckScript-Regular.ttf"
check "Marck Script OFL.txt"               "test -f assets/fonts/marck-script/OFL.txt"
check "Cormorant OFL is real OFL-1.1"      "grep -q 'SIL OPEN FONT LICENSE' assets/fonts/cormorant-garamond/OFL.txt"
check "Marck Script OFL is real OFL-1.1"   "grep -q 'SIL OPEN FONT LICENSE' assets/fonts/marck-script/OFL.txt"
check "Fonts smoke-test exists"            "test -f assets/fonts/SMOKE.md"
echo

# --- Pytest gate (full marketing-v3 test suite) ---
echo "[GATE] Full pytest"
PYTEST_OUT=$(PYTHONPATH=. $PYTEST -q --no-header 2>&1 | tail -3)
echo "$PYTEST_OUT"
PASSED_COUNT=$(echo "$PYTEST_OUT" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+')
if [ -n "${PASSED_COUNT:-}" ] && [ "$PASSED_COUNT" -ge 111 ]; then
    echo "  PASS  pytest passed=$PASSED_COUNT (>=111)"
    PASSES=$((PASSES + 1))
else
    echo "  FAIL  pytest passed=${PASSED_COUNT:-?} (need >=111)"
    FAILS=$((FAILS + 1))
fi
echo

# --- GH Secrets check (требует gh auth) ---
echo "[Gate 7-A] GH Secrets ELEVENLABS_*"
if command -v gh >/dev/null 2>&1; then
    if gh secret list --repo Carbon1777/forton-lab-marketing 2>/dev/null | grep -q '^ELEVENLABS_API_KEY'; then
        echo "  PASS  ELEVENLABS_API_KEY in GH Secrets"
        PASSES=$((PASSES + 1))
    else
        echo "  FAIL  ELEVENLABS_API_KEY NOT in GH Secrets — run runbook 07-secrets-bootstrap-elevenlabs.md"
        FAILS=$((FAILS + 1))
    fi
    if gh secret list --repo Carbon1777/forton-lab-marketing 2>/dev/null | grep -q '^ELEVENLABS_TIER'; then
        echo "  PASS  ELEVENLABS_TIER in GH Secrets"
        PASSES=$((PASSES + 1))
    else
        echo "  FAIL  ELEVENLABS_TIER NOT in GH Secrets — set to 'starter'"
        FAILS=$((FAILS + 1))
    fi
else
    echo "  SKIP  gh CLI not available — verify manually"
fi
echo

# --- Sub-repo PRs merged (informational, not blocking) ---
echo "[INFO] Recent main commits on forton-lab-marketing"
git log --oneline -5 origin/main | sed 's/^/        /'
echo

# --- Summary ---
echo "=== Summary ==="
echo "  Passes: $PASSES"
echo "  Fails:  $FAILS"
if [ "$FAILS" -eq 0 ]; then
    echo
    echo "Phase 7 ACCEPTANCE: GREEN — ready to close."
    exit 0
else
    echo
    echo "Phase 7 ACCEPTANCE: RED — see fails above."
    exit 1
fi
