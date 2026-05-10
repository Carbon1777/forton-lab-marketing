"""BOOT-02: tier helper tests."""

from __future__ import annotations

import pytest

from src.elevenlabs_tier import (
    PAID_TIERS,
    KNOWN_FREE_TIERS,
    DEFAULT_TIER,
    TierMissingError,
    get_studio_tier,
    is_paid_tier,
    require_paid_tier,
)


def test_default_tier_is_starter(monkeypatch):
    """BOOT-02: default fallback = 'starter' если env отсутствует."""
    monkeypatch.delenv("ELEVENLABS_TIER", raising=False)
    assert get_studio_tier() == "starter"


def test_env_override(monkeypatch):
    """BOOT-02: ELEVENLABS_TIER=creator → get_studio_tier() == 'creator'."""
    monkeypatch.setenv("ELEVENLABS_TIER", "creator")
    assert get_studio_tier() == "creator"


def test_env_strip_and_lowercase(monkeypatch):
    """BOOT-02: case-insensitive + whitespace-trimmed."""
    monkeypatch.setenv("ELEVENLABS_TIER", "  STARTER  ")
    assert get_studio_tier() == "starter"


@pytest.mark.parametrize("tier", sorted(PAID_TIERS))
def test_paid_tiers_whitelist(tier):
    """BOOT-02: все paid tiers — True."""
    assert is_paid_tier(tier) is True


@pytest.mark.parametrize("tier", sorted(KNOWN_FREE_TIERS))
def test_free_tiers_return_false(tier):
    """BOOT-02: free/trial/grant — False (strict v1)."""
    assert is_paid_tier(tier) is False


def test_case_insensitive_paid():
    assert is_paid_tier("STARTER") is True
    assert is_paid_tier(" creator ") is True
    assert is_paid_tier("Pro") is True


def test_empty_or_none_false():
    assert is_paid_tier("") is False
    assert is_paid_tier(None) is False


def test_unknown_tier_warns_and_denies(capsys):
    """BOOT-02: unknown tier → False + stderr WARN."""
    assert is_paid_tier("mystery_tier_2099") is False
    err = capsys.readouterr().err
    assert "unknown ElevenLabs tier" in err
    assert "mystery_tier_2099" in err


def test_require_paid_tier_raises_on_free(monkeypatch):
    """BOOT-02: require_paid_tier() raises TierMissingError на free."""
    monkeypatch.setenv("ELEVENLABS_TIER", "free")
    with pytest.raises(TierMissingError):
        require_paid_tier()


def test_require_paid_tier_returns_tier_on_paid(monkeypatch):
    """BOOT-02: require_paid_tier() returns tier на paid subscription."""
    monkeypatch.setenv("ELEVENLABS_TIER", "starter")
    assert require_paid_tier() == "starter"


def test_default_constant_is_paid():
    """BOOT-02: DEFAULT_TIER должен быть в PAID_TIERS, иначе default fallback ломает gate."""
    assert DEFAULT_TIER in PAID_TIERS
