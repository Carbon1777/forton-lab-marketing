#!/usr/bin/env bash
# Phase 10 — Voice Selection acceptance gate
# Verifies VOICE-01..03 + BOOT-01/02 + character.yaml additivity

set +e
PASSES=0
FAILS=0

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHAR_YAML="$REPO_ROOT/ai_talent/character.yaml"

check() {
    local name="$1"; shift
    if eval "$@" > /dev/null 2>&1; then
        echo "  PASS  $name"
        PASSES=$((PASSES + 1))
    else
        echo "  FAIL  $name"
        FAILS=$((FAILS + 1))
    fi
}

echo "[VOICE-01] character.yaml.voice locked"
check "voice.status == ready"             "grep -A1 '^voice:' '$CHAR_YAML' | grep -q 'status: ready'"
check "voice.voice_id present"            "grep -A10 '^voice:' '$CHAR_YAML' | grep -q 'voice_id: GN4wbsbejSnGSa1AzjH5'"
check "voice.voice_name set"              "grep -A10 '^voice:' '$CHAR_YAML' | grep -qi 'ekaterina'"
check "voice.language ru"                 "grep -A10 '^voice:' '$CHAR_YAML' | grep -q 'language: ru'"
check "voice.model_id multilingual_v2"    "grep -A20 '^voice:' '$CHAR_YAML' | grep -q 'model_id: eleven_multilingual_v2'"
check "voice.reference_samples >=3"       "test \$(grep -A30 '^voice:' '$CHAR_YAML' | grep -c 'voice-reference/ekaterina') -ge 3"

echo ""
echo "[VOICE-02] voice_settings split"
check "centry.stability == 0.4"           "grep -A40 '^voice:' '$CHAR_YAML' | grep -A5 'centry:' | grep -q 'stability: 0.4'"
check "diktum.stability == 0.7"           "grep -A50 '^voice:' '$CHAR_YAML' | grep -A5 'diktum:' | grep -q 'stability: 0.7'"
check "style: 0.0 (PIT-3)"                "! grep -A50 '^voice:' '$CHAR_YAML' | grep -qE 'style: 0\.[12]'"

echo ""
echo "[VOICE-03] emotional text cues documented"
check "text_cues_supported list"          "grep -A60 '^voice:' '$CHAR_YAML' | grep -q 'text_cues_supported:'"

echo ""
echo "[BOOT-01] reference samples present on disk"
check "ekaterina_01_neutral.mp3"          "test -f '$REPO_ROOT/assets/voice-reference/ekaterina_01_neutral.mp3'"
check "ekaterina_02_centry.mp3"           "test -f '$REPO_ROOT/assets/voice-reference/ekaterina_02_centry.mp3'"
check "ekaterina_03_diktum.mp3"           "test -f '$REPO_ROOT/assets/voice-reference/ekaterina_03_diktum.mp3'"

echo ""
echo "[ADDITIVITY] phase_8 + lora blocks preserved"
check "phase_8.selected_variant == variant_2"  "grep -A5 '^phase_8:' '$CHAR_YAML' | grep -q 'selected_variant: variant_2'"
check "lora.status == ready"                   "grep -A5 '^lora:' '$CHAR_YAML' | grep -q 'status: ready'"
check "lora.trigger_word == OHWX_FORTONA"      "grep -A10 '^lora:' '$CHAR_YAML' | grep -q 'OHWX_FORTONA'"

echo ""
echo "=== Summary ==="
echo "  Passes: $PASSES"
echo "  Fails:  $FAILS"
if [ "$FAILS" -eq 0 ]; then
    echo ""
    echo "Phase 10 ACCEPTANCE: GREEN — ready to close."
    exit 0
else
    echo ""
    echo "Phase 10 ACCEPTANCE: RED — see fails above."
    exit 1
fi
