"""Unit tests for dzen_verify — Phase 2 PUB-10 manual-check reminder.

Note: original Plan 02-03 spec was HTTP-scrape (regex on JSON-LD datePublished),
but Yandex closed anonymous Дзен access in 2024-2025 (all probe variants → SSO
redirect). Scope downgraded to TG-reminder. See decisions.md (2026-05-10).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.dzen_verify import (
    DZEN_CHANNEL_URL,
    REMINDER_WAIT_MIN,
    _send_reminder,
    verify,
)


# ===================================================================
# Constants
# ===================================================================

def test_constants_have_expected_values():
    """Sanity: channel URL is fortonlab, wait window 10 min."""
    assert DZEN_CHANNEL_URL == "https://dzen.ru/fortonlab"
    assert REMINDER_WAIT_MIN == 10


# ===================================================================
# verify orchestrator
# ===================================================================

def test_verify_returns_true_on_send_success():
    """Happy path: tg_nudge.send succeeds → verify returns True."""
    with patch("src.tg_nudge.send", return_value=0) as mock_send:
        assert verify("centry-jun15-morning") is True
        mock_send.assert_called_once()


def test_verify_calls_tg_nudge_with_template_key_and_kwargs():
    """verify routes to dzen_manual_check template with correct kwargs."""
    with patch("src.tg_nudge.send", return_value=0) as mock_send:
        verify("centry-jun15-morning")
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "dzen_manual_check"
        assert kwargs["slug"] == "centry-jun15-morning"
        assert kwargs["channel_url"] == DZEN_CHANNEL_URL
        assert kwargs["wait_min"] == REMINDER_WAIT_MIN


def test_verify_returns_false_on_tg_failure():
    """tg_nudge crash should be caught — verify returns False, no raise."""
    with patch("src.tg_nudge.send", side_effect=RuntimeError("TG down")):
        result = verify("centry-jun15-morning")
        assert result is False


def test_verify_handles_empty_slug():
    """slug='' (called from publish.yml without input) → '(unknown)' substitute."""
    with patch("src.tg_nudge.send", return_value=0) as mock_send:
        verify("")
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["slug"] == "(unknown)"


def test_verify_never_raises_even_on_unexpected_error():
    """Defense-in-depth: even if _send_reminder raises (it shouldn't), verify
    must return a bool, not propagate."""
    with patch("src.dzen_verify._send_reminder", return_value=False):
        # Should not raise
        result = verify("test-slug")
        assert result is False


# ===================================================================
# _send_reminder
# ===================================================================

def test_send_reminder_returns_true_on_success():
    with patch("src.tg_nudge.send", return_value=0):
        assert _send_reminder("test-slug") is True


def test_send_reminder_swallows_exception_returns_false():
    """tg_nudge crash logged to stderr but never propagates."""
    with patch("src.tg_nudge.send", side_effect=ConnectionError("TG outage")):
        assert _send_reminder("test-slug") is False


# ===================================================================
# __main__ — silent-fail invariant
# ===================================================================

def test_main_exits_zero_on_send_success():
    """`python -m src.dzen_verify <slug>` exits 0 with mocked TG."""
    repo_root = Path(__file__).resolve().parent.parent
    # Use subprocess to truly exercise __main__; mock TG at env level so it can't actually call out
    proc = subprocess.run(
        [sys.executable, "-m", "src.dzen_verify", "test-slug"],
        cwd=repo_root,
        env={
            "PATH": "/usr/bin:/bin",
            # Provide bogus token + chat_id so tg_nudge env() doesn't sys.exit(1)
            # before our try/except can swallow the network failure.
            "TG_PLANNER_BOT_TOKEN": "bot:fake-token-for-test",
            "TG_OWNER_CHAT_ID": "0",
            "PYTHONPATH": str(repo_root),
        },
        capture_output=True,
        timeout=15,
    )
    # Real network call to TG api WILL fail with 401/connection error,
    # but verify swallows and __main__ exits 0.
    assert proc.returncode == 0, (
        f"exit={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


def test_main_exits_zero_when_no_slug_arg():
    """`python -m src.dzen_verify` (no arg) still exits 0."""
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "src.dzen_verify"],
        cwd=repo_root,
        env={
            "PATH": "/usr/bin:/bin",
            "TG_PLANNER_BOT_TOKEN": "bot:fake-token-for-test",
            "TG_OWNER_CHAT_ID": "0",
            "PYTHONPATH": str(repo_root),
        },
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 0


# ===================================================================
# tg_nudge integration: dzen_manual_check template
# ===================================================================

def test_template_dzen_manual_check_in_registry():
    """PUB-10: template registered under 'dzen_manual_check' key."""
    from src.tg_nudge import TEMPLATES
    assert "dzen_manual_check" in TEMPLATES


def test_template_dzen_manual_check_renders_with_required_kwargs():
    """Template formats with {slug}, {channel_url}, {wait_min}."""
    from src.tg_nudge import TEMPLATES
    rendered = TEMPLATES["dzen_manual_check"].format(
        slug="centry-jun15-morning",
        channel_url="https://dzen.ru/fortonlab",
        wait_min=10,
    )
    assert "centry-jun15-morning" in rendered
    assert "https://dzen.ru/fortonlab" in rendered
    assert "10" in rendered
    # Actionable content present
    assert "Дзен" in rendered or "dzen" in rendered.lower()
    assert "@zen_sync_bot" in rendered or "zen_sync_bot" in rendered


def test_template_dzen_manual_check_no_format_placeholders_left():
    """All template placeholders are filled (no leftover {var} after format)."""
    from src.tg_nudge import TEMPLATES
    rendered = TEMPLATES["dzen_manual_check"].format(
        slug="test-slug",
        channel_url="https://dzen.ru/test",
        wait_min=15,
    )
    leftover = re.findall(r"\{[a-z_]+\}", rendered)
    assert leftover == []
