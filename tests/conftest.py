"""Pytest fixtures shared across Phase 1 tests."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent  # marketing-v3/
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def repo_root() -> Path:
    """Real marketing-v3/ root — used to resolve .lint/forbidden_words.txt."""
    return REPO_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def forbidden_words_file() -> Path:
    """Path to real Phase 0 Plan 02 forbidden_words.txt (28 entries)."""
    p = REPO_ROOT / ".lint" / "forbidden_words.txt"
    assert p.exists(), f"missing Phase 0 deliverable: {p}"
    return p


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Materialise a minimal marketing-v3 layout in tmp_path:
        .lint/forbidden_words.txt (copied from real)
        plans/
        .metrics/api_spend.json (fresh)
        assets/  media/
    """
    (tmp_path / ".lint").mkdir()
    shutil.copy(REPO_ROOT / ".lint" / "forbidden_words.txt",
                tmp_path / ".lint" / "forbidden_words.txt")
    (tmp_path / "plans").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "media").mkdir()
    metrics = tmp_path / ".metrics"
    metrics.mkdir()
    (metrics / "api_spend.json").write_text(
        json.dumps({"_schema_version": 1, "_updated": None}, indent=2)
    )
    return tmp_path


@pytest.fixture
def sample_plan_text() -> str:
    """Valid 3-day mini-plan in canonical sectional Markdown format (variant b)."""
    return (FIXTURES_DIR / "valid_plan.md").read_text(encoding="utf-8")


@pytest.fixture
def broken_plan_text() -> str:
    """Plan with malformed YAML in one section."""
    return (FIXTURES_DIR / "broken_yaml_plan.md").read_text(encoding="utf-8")


@pytest.fixture
def mock_anthropic_client():
    """MagicMock that mimics anthropic.Anthropic() return value.
    Use: client.messages.create.return_value = <fake_msg>.
    Test must set the return_value before calling generator.
    """
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(type="text", text="<set me in test>")]
    fake_msg.stop_reason = "end_turn"
    fake_msg.usage = MagicMock(input_tokens=1000, output_tokens=500)

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg
    return fake_client, fake_msg


# ============================================================
# Phase 1.5 fixtures — PTB Application / CallbackQuery mocks
# ============================================================

import datetime as _dt_15
from unittest.mock import AsyncMock as _AsyncMock_15, MagicMock as _MagicMock_15


@pytest.fixture
def mock_owner_id() -> int:
    """Synthetic Telegram chat_id used as owner across handler tests."""
    return 12345


@pytest.fixture
def sample_plan_text_30days() -> str:
    """Valid 30-day plan со status=draft (covers 5 ISO weeks of June 2026)."""
    return (FIXTURES_DIR / "sample_plan_2026-06.md").read_text(encoding="utf-8")


@pytest.fixture
def mock_query(mock_owner_id):
    """Construct a minimal CallbackQuery for handler tests.

    Defaults to data='approve:deadbeef' — tests override via `mock_query.data = ...`.
    All TG-side I/O methods are AsyncMock (PTB 21.x is async-only).
    """
    from telegram import CallbackQuery, Chat, Message, User

    user = User(id=mock_owner_id, first_name="Forton", is_bot=False)
    chat = Chat(id=mock_owner_id, type="private")
    msg = Message(
        message_id=999,
        date=_dt_15.datetime.now(_dt_15.timezone.utc),
        chat=chat,
        from_user=user,
    )
    # PTB v21 Message.reply_text is async — mock it
    msg.reply_text = _AsyncMock_15()

    q = _MagicMock_15(spec=CallbackQuery)
    q.from_user = user
    q.message = msg
    q.id = "test_callback_id"
    q.data = "approve:deadbeef"
    q.answer = _AsyncMock_15(return_value=True)
    q.edit_message_text = _AsyncMock_15()
    q.edit_message_reply_markup = _AsyncMock_15()
    return q


@pytest.fixture
def mock_ctx(mock_owner_id, tmp_path):
    """ContextTypes-like object with bot_data + stop_running mock.

    bot_data['repo_root'] points to tmp_path so handlers writing files
    don't pollute the real working tree. Handlers needing a plan file
    should create one under tmp_path / 'plans' / f'monthly_plan_{...}.md'.
    """
    ctx = _MagicMock_15()
    ctx.application = _MagicMock_15()
    ctx.application.bot_data = {
        "owner_chat_id": mock_owner_id,
        "repo_root": tmp_path,
    }
    ctx.application.stop_running = _MagicMock_15()
    return ctx


@pytest.fixture
def tmp_repo_with_draft_plan(tmp_repo, sample_plan_text_30days):
    """Extends tmp_repo by writing sample plan as plans/monthly_plan_2026-06.md.
    Used by approve/reject/sha-verify tests."""
    plan = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    plan.write_text(sample_plan_text_30days, encoding="utf-8")
    return tmp_repo, plan
