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

    NOTE: PTB ``telegram.Message`` is a frozen dataclass-style object that
    forbids attribute assignment, so ``query.message`` is a plain ``MagicMock``
    (not a real Message instance) — handlers only touch ``query.message.reply_text``
    which is provided as an AsyncMock.
    """
    from telegram import CallbackQuery, User

    user = User(id=mock_owner_id, first_name="Forton", is_bot=False)
    msg = _MagicMock_15()
    msg.reply_text = _AsyncMock_15()
    msg.message_id = 999

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


# ============================================================
# Phase 2 fixtures — preview_bot / daily_post_generator / dzen_verify
# ============================================================

from unittest.mock import AsyncMock as _AsyncMock_p2, MagicMock as _MagicMock_p2, patch as _patch_p2


@pytest.fixture
def tmp_drafts_dir(tmp_path):
    """Path to drafts/ inside tmp_path (created on first use)."""
    d = tmp_path / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mock_bot_send():
    """Patch PTB Bot.send_photo / send_video / send_message / send_media_group /
    edit_message_reply_markup as AsyncMock instances. Returns dict for assertions.

    Each send_* mock returns MagicMock(message_id=N) where N differs per type
    (photo=1001, video=1002, message=1003, media_group=[(1004,)]) so tests can
    verify which API was called via message_id.
    """
    with _patch_p2("telegram.Bot.send_photo", new_callable=_AsyncMock_p2) as mp, \
         _patch_p2("telegram.Bot.send_video", new_callable=_AsyncMock_p2) as mv, \
         _patch_p2("telegram.Bot.send_message", new_callable=_AsyncMock_p2) as mm, \
         _patch_p2("telegram.Bot.send_media_group", new_callable=_AsyncMock_p2) as mg, \
         _patch_p2("telegram.Bot.edit_message_reply_markup", new_callable=_AsyncMock_p2) as me:
        mp.return_value = _MagicMock_p2(message_id=1001)
        mv.return_value = _MagicMock_p2(message_id=1002)
        mm.return_value = _MagicMock_p2(message_id=1003)
        mg.return_value = [_MagicMock_p2(message_id=1004), _MagicMock_p2(message_id=1005)]
        me.return_value = _MagicMock_p2(message_id=1003)
        yield {
            "photo": mp,
            "video": mv,
            "message": mm,
            "media_group": mg,
            "edit_reply_markup": me,
        }


@pytest.fixture
def mock_anthropic_regen():
    """Patch daily_post_generator.generate to return canned regen output.

    Used by Plan 02 tests for regen_one(); returns (text, in_tokens, out_tokens).
    Tests can override via `mock_anthropic_regen.return_value = (custom, 800, 400)`.

    NOTE: imports daily_post_generator lazily — module not yet exists in Wave 0,
    Plan 02 creates it. Fixture only resolves at test-call time.
    """
    with _patch_p2("src.daily_post_generator.generate", create=True) as gen:
        gen.return_value = ("Новый текст после правки. centryweb.ru", 800, 400)
        yield gen


@pytest.fixture
def long_caption_draft(tmp_path):
    """Fixture for T-2-08 — body 1500+ chars > 1024 caption limit triggers split fallback.

    Returns tuple (draft_path, image_path) — frontmatter draft with image: field
    and a fake 100-byte PNG.
    """
    import frontmatter as _fm
    body = "Слово недели «как бы». " * 60   # ≈ 1380 chars
    if len(body) < 1100:
        body += " centryweb.ru " * 5
    assert len(body) > 1024, f"body too short for T-2-08 fixture: {len(body)} chars"

    image_path = tmp_path / "assets" / "long_caption_test.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    draft_path = tmp_path / "drafts" / "diktum-long-caption.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    post = _fm.Post(
        content=body,
        slug="diktum-long-caption",
        channels=["tg", "vk"],
        product="diktum",
        rubric="words_of_week",
        media=[{"path": "assets/long_caption_test.png", "sha256": "deadbeef", "role": "image"}],
        image="assets/long_caption_test.png",
        generated_at="2026-06-15T09:00:00Z",
        status="draft",
        daily_regen_count=0,
        plan_date="2026-06-15",
    )
    draft_path.write_text(_fm.dumps(post), encoding="utf-8")
    return draft_path, image_path


@pytest.fixture
def short_caption_draft(tmp_path):
    """Counterpart fixture: body < 1024 chars triggers normal send_photo with caption."""
    import frontmatter as _fm
    body = "Утренняя подборка кафе. centryweb.ru"
    image_path = tmp_path / "assets" / "short_caption_test.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    draft_path = tmp_path / "drafts" / "centry-short-caption.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    post = _fm.Post(
        content=body,
        slug="centry-short-caption",
        channels=["tg", "vk"],
        product="centry",
        rubric="city_picks",
        media=[{"path": "assets/short_caption_test.png", "sha256": "cafe1234", "role": "image"}],
        image="assets/short_caption_test.png",
        generated_at="2026-06-15T09:00:00Z",
        status="draft",
        daily_regen_count=0,
        plan_date="2026-06-15",
    )
    draft_path.write_text(_fm.dumps(post), encoding="utf-8")
    return draft_path, image_path


@pytest.fixture
def multi_entry_plan(fixtures_dir, tmp_path):
    """Reads sample_plan_2026-06-multi.md (3 entries on 2026-06-15 + 1+1).

    Writes copy to tmp_path/plans/monthly_plan_2026-06.md. Returns (plan_path, fixture_text).
    """
    fixture_text = (fixtures_dir / "sample_plan_2026-06-multi.md").read_text(encoding="utf-8")
    plan_path = tmp_path / "plans" / "monthly_plan_2026-06.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(fixture_text, encoding="utf-8")
    return plan_path, fixture_text
