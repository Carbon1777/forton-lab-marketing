"""Phase 9 W-001 — preflight/record ordering carry-forward regression.

Background (Phase 9 audit):
    src/ai_talent/lora_trainer.train_v1 calls preflight_check BEFORE
    trainings.create but record_provider_spend only AFTER training.status
    == "succeeded". If the operator killed the process between create + success,
    the $2.20 spend was NOT recorded => no Replicate billing API to auto-recover.

Decision (Phase 11 Plan 11-01 pre-locked):
    Do NOT auto-fix train_v1 (Replicate billing endpoint is private/undocumented).
    Instead, document the gap via 3 CI-blocking tests + manual recovery path in
    RUNBOOK_PHASE11.md "Recovery Paths" section.

This test guards three properties:
    1. SUCCESS path records $2.20 (no regression).
    2. FAILURE path does NOT record (Replicate doesn't charge for failed runs).
    3. Manual recovery hook (record_provider_spend) is still callable — if a
       future refactor removes/renames this API, this test fails and alerts
       the operator that the W-001 runbook recipe is broken.

Mitigation status: ACCEPTED — gap documented + recovery hook tested.
Follow-up ticket: Phase 11 W-001 (v1.2 backlog) — wrap train_v1 in try/finally
once Replicate exposes billing.list endpoint (currently private).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.spend_tracker_v2 import (
    read_provider_spend,
    record_provider_spend,
)
from src.ai_talent import lora_trainer


def _current_month() -> str:
    return dt.date.today().strftime("%Y-%m")


# ---------------------------------------------------------------------
# Helpers — synthetic Replicate training objects (no network)
# ---------------------------------------------------------------------

def _make_training(status: str, *, error: str | None = None):
    t = MagicMock()
    t.id = f"fake_training_{status}"
    t.status = status
    t.error = error
    if status == "succeeded":
        t.output = {
            "version": "carbon1777/forton-lab-character-v1:"
                       "5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1",
        }
        t.urls = {
            "get": ("https://replicate.com/carbon1777/forton-lab-character-v1/"
                    "versions/5d950b9d38b55d13c5ebf1ed2a086f269a3663b9e9244b82b6984bf79ffb3ca1")
        }
    else:
        t.output = None
        t.urls = {}
    return t


@pytest.fixture
def fake_succeeded_training():
    return _make_training("succeeded")


@pytest.fixture
def fake_failed_training():
    return _make_training("failed", error="Cuda OOM")


@pytest.fixture
def mock_replicate_with_training(fake_succeeded_training):
    """Replicate client that returns a synthetic succeeded training.

    Plus mocked .models.create/.get/.versions.list — train_v1 walks each.
    """
    client = MagicMock()
    client.models.create.return_value = MagicMock(
        owner="testowner", name="forton-lab-character-v1")

    # client.models.get(TRAINER_REF).versions.list() — list of objects with .id
    trainer_model = MagicMock()
    trainer_version = MagicMock()
    trainer_version.id = "deadbeef" + "0" * 24
    trainer_model.versions.list.return_value = [trainer_version]
    client.models.get.return_value = trainer_model

    client.trainings.create.return_value = fake_succeeded_training
    client.trainings.get.return_value = fake_succeeded_training
    return client


@pytest.fixture
def mock_replicate_with_failed_training(fake_failed_training):
    client = MagicMock()
    client.models.create.return_value = MagicMock(
        owner="testowner", name="forton-lab-character-v1")
    trainer_model = MagicMock()
    trainer_version = MagicMock()
    trainer_version.id = "cafe0001" + "0" * 24
    trainer_model.versions.list.return_value = [trainer_version]
    client.models.get.return_value = trainer_model
    client.trainings.create.return_value = fake_failed_training
    client.trainings.get.return_value = fake_failed_training
    return client


# ---------------------------------------------------------------------
# Test 1 — SUCCESS path records $2.20
# ---------------------------------------------------------------------

def test_w001_spend_recorded_on_success(
    tmp_spend_file: Path,
    mock_replicate_with_training,
    monkeypatch,
    fake_succeeded_training,
    tmp_path,
):
    """SUCCESS path: record_provider_spend WAS called → spend >= $2.20."""
    # Bypass real polling — return succeeded synchronously
    monkeypatch.setattr(
        lora_trainer, "poll_training",
        lambda training_id, **kw: fake_succeeded_training,
    )

    result = lora_trainer.train_v1(
        owner="testowner",
        input_images_url="https://example.test/dataset.zip",
        spend_file=tmp_spend_file,
        cache_dir=tmp_path / "lora_cache",
        client=mock_replicate_with_training,
    )

    spent = read_provider_spend(tmp_spend_file, _current_month(), "replicate")
    assert spent == pytest.approx(2.20, abs=0.001), (
        f"W-001 regression: expected $2.20 recorded after success, got ${spent}"
    )
    assert result.get("training_id"), "train_v1 must return training metadata"
    assert result.get("trigger_word") == "OHWX_FORTONA"
    assert result.get("actual_cost_usd") == pytest.approx(2.20, abs=0.001)


# ---------------------------------------------------------------------
# Test 2 — FAILURE path does NOT record (Replicate doesn't charge)
# ---------------------------------------------------------------------

def test_w001_spend_NOT_recorded_on_failure(
    tmp_spend_file: Path,
    mock_replicate_with_failed_training,
    monkeypatch,
    fake_failed_training,
    tmp_path,
):
    """FAILED path: training failed → no charge → no spend recorded."""
    monkeypatch.setattr(
        lora_trainer, "poll_training",
        lambda training_id, **kw: fake_failed_training,
    )

    with pytest.raises(RuntimeError, match=r"(?i)training\s+failed|cuda|failed"):
        lora_trainer.train_v1(
            owner="testowner",
            input_images_url="https://example.test/dataset.zip",
            spend_file=tmp_spend_file,
            cache_dir=tmp_path / "lora_cache_fail",
            client=mock_replicate_with_failed_training,
        )

    spent = read_provider_spend(tmp_spend_file, _current_month(), "replicate")
    assert spent == pytest.approx(0.0, abs=0.001), (
        f"W-001 regression: failed training must NOT record spend — got ${spent}. "
        "Replicate billing endpoint is private; auto-recovery impossible. "
        "If this test starts failing, train_v1 began charging on failures — "
        "investigate before commit."
    )


# ---------------------------------------------------------------------
# Test 3 — Manual recovery hook remains callable
# ---------------------------------------------------------------------

def test_w001_carry_forward_safety_net(tmp_spend_file: Path):
    """Manual recovery hook must remain callable for the RUNBOOK_PHASE11.md recipe.

    If poll_training raises mid-flight (network drop, process kill, OOM after
    training succeeded but before record_provider_spend), the operator's only
    recovery is the direct record_provider_spend() call documented in
    RUNBOOK_PHASE11.md "Recovery Paths". This test guards that API surface —
    if a future refactor renames/removes the function or changes its signature,
    this test fails BEFORE the runbook recipe silently breaks in production.
    """
    record_provider_spend(
        tmp_spend_file,
        "replicate",
        usd=2.20,
        units=1800,
        unit_field="predict_seconds",
    )
    spent = read_provider_spend(tmp_spend_file, _current_month(), "replicate")
    assert spent == pytest.approx(2.20, abs=0.001), (
        "Manual recovery API broken — RUNBOOK_PHASE11.md recipe must be updated"
    )

    # Verify the spend file is well-formed JSON (no schema corruption)
    data = json.loads(tmp_spend_file.read_text(encoding="utf-8"))
    assert data.get("_schema_version") == 3, "schema_version must remain 3 after manual write"
