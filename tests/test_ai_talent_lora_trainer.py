"""Phase 9 Plan 02: lora_trainer unit tests (no live Replicate calls)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ai_talent.lora_trainer import (
    AUTOCAPTION,
    LEARNING_RATE,
    LORA_RANK,
    OPTIMIZER,
    POLL_INTERVAL_SEC,
    STEPS_CAP,
    TRIGGER_WORD_LOCKED,
    build_training_input,
    extract_result_version,
    poll_training,
    train_v1,
)


# ---------- Test 1: locked params ----------
def test_build_training_input_locked_params():
    """All input fields must match CHAR-04 / RESEARCH §Pattern 1 invariants."""
    inp = build_training_input(input_images_url="https://x/v1.zip", steps=1000)
    assert inp["trigger_word"] == TRIGGER_WORD_LOCKED == "OHWX_FORTONA"
    assert inp["steps"] == 1000
    assert inp["lora_rank"] == LORA_RANK == 16
    assert inp["learning_rate"] == LEARNING_RATE == 0.0004
    assert inp["optimizer"] == OPTIMIZER == "adamw8bit"
    assert inp["autocaption"] is AUTOCAPTION is False
    assert inp["input_images"] == "https://x/v1.zip"


# ---------- Test 2: steps cap ----------
def test_steps_cap_enforced():
    """steps > STEPS_CAP must raise ValueError mentioning the cap."""
    with pytest.raises(ValueError, match=r"1500"):
        build_training_input(input_images_url="https://x/v1.zip", steps=1600)


# ---------- Test 3: poll state machine — succeeded ----------
def test_poll_state_machine_succeeds():
    """poll_training returns the training once status is 'succeeded'."""
    client = MagicMock()
    states = ["processing", "processing", "succeeded"]
    client.trainings.get.side_effect = [
        SimpleNamespace(id="t1", status=s) for s in states
    ]
    with patch("src.ai_talent.lora_trainer.time.sleep") as sleep_mock:
        result = poll_training("t1", client=client, poll_interval=1, timeout_sec=999)
    assert result.status == "succeeded"
    assert client.trainings.get.call_count == 3
    assert sleep_mock.call_count == 2


# ---------- Test 4: poll state machine — timeout cancels ----------
def test_poll_state_machine_timeout():
    """Polling past timeout calls trainings.cancel and raises TimeoutError."""
    client = MagicMock()
    client.trainings.get.return_value = SimpleNamespace(id="t1", status="processing")
    with patch("src.ai_talent.lora_trainer.time.sleep"), \
         patch("src.ai_talent.lora_trainer.time.monotonic", side_effect=[0, 100, 9999]):
        with pytest.raises(TimeoutError, match="canceled"):
            poll_training("t1", client=client, poll_interval=1, timeout_sec=50)
    client.trainings.cancel.assert_called_once_with("t1")


# ---------- Test 5: extract result version ----------
def test_extract_result_version_from_output_dict():
    """If training.output has 'version' key, return it as-is."""
    training = SimpleNamespace(
        output={"version": "carbon1777/forton-lab-character-v1:abc123def"},
        urls={},
    )
    assert extract_result_version(training) == "carbon1777/forton-lab-character-v1:abc123def"


def test_extract_result_version_url_fallback():
    """If output empty, fall back to parsing get-URL."""
    training = SimpleNamespace(
        output=None,
        urls={"get": "https://replicate.com/carbon1777/forton-lab-character-v1/versions/deadbeef1234"},
    )
    assert extract_result_version(training) == "carbon1777/forton-lab-character-v1:deadbeef1234"


# ---------- Test 6: BOOT-01 spend gate ordering ----------
def test_train_v1_boot01_invariant(tmp_path):
    """preflight_check BEFORE trainings.create; record_provider_spend AFTER success."""
    spend_file = tmp_path / "spend.json"
    spend_file.write_text(json.dumps({"_schema_version": 3, "2026-05": {"by_provider": {}}}))

    client = MagicMock()
    client.models.get.return_value.versions.list.return_value = [
        SimpleNamespace(id="trainer-sha-xyz")
    ]
    succeeded = SimpleNamespace(
        id="trn-abc",
        status="succeeded",
        output={"version": "carbon1777/forton-lab-character-v1:result-sha"},
        urls={},
        error=None,
    )
    client.trainings.create.return_value = SimpleNamespace(id="trn-abc")
    client.trainings.get.return_value = succeeded

    call_log: list[str] = []

    def preflight_spy(*args, **kwargs):
        call_log.append("preflight")

    def record_spy(*args, **kwargs):
        call_log.append("record")

    def create_spy(*args, **kwargs):
        call_log.append("create")
        return SimpleNamespace(id="trn-abc")

    client.trainings.create.side_effect = create_spy

    with patch("src.ai_talent.lora_trainer.preflight_check", side_effect=preflight_spy), \
         patch("src.ai_talent.lora_trainer.record_provider_spend", side_effect=record_spy), \
         patch("src.ai_talent.lora_trainer.time.sleep"):
        result = train_v1(
            owner="carbon1777",
            input_images_url="https://x/v1.zip",
            steps=1000,
            spend_file=spend_file,
            cache_dir=tmp_path / "cache",
            client=client,
        )

    # BOOT-01 ordering: preflight FIRST, create after, record LAST
    assert call_log == ["preflight", "create", "record"], call_log
    assert result["trigger_word"] == "OHWX_FORTONA"
    assert result["steps"] == 1000
    assert result["rank"] == 16


# ---------- Test 7: training failure raises ----------
def test_train_v1_failure_raises(tmp_path):
    """If training.status != succeeded, train_v1 raises RuntimeError (no record_spend)."""
    spend_file = tmp_path / "spend.json"
    spend_file.write_text(json.dumps({"_schema_version": 3, "2026-05": {"by_provider": {}}}))

    client = MagicMock()
    client.models.get.return_value.versions.list.return_value = [
        SimpleNamespace(id="trainer-sha")
    ]
    failed = SimpleNamespace(
        id="trn-fail",
        status="failed",
        output=None,
        urls={},
        error="OOM",
    )
    client.trainings.create.return_value = SimpleNamespace(id="trn-fail")
    client.trainings.get.return_value = failed

    record_called: list = []
    with patch("src.ai_talent.lora_trainer.preflight_check"), \
         patch(
            "src.ai_talent.lora_trainer.record_provider_spend",
            side_effect=lambda *a, **k: record_called.append(True),
         ), \
         patch("src.ai_talent.lora_trainer.time.sleep"):
        with pytest.raises(RuntimeError, match="training failed"):
            train_v1(
                owner="carbon1777",
                input_images_url="https://x/v1.zip",
                spend_file=spend_file,
                cache_dir=tmp_path / "cache",
                client=client,
            )

    # No spend recorded on failure
    assert record_called == []
