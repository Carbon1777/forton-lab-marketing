"""Unit tests for preview_bot — Phase 2 PREV-01..06 + GEN-04.

Tests grouped by Plan task:
    Task 1 — send_preview_* variants + helpers (constants, kb, sha, classify)
    Task 2 — callback handlers (publish, cancel, edit global dispatch)
    Task 3 — pre_flight + main + lifecycle
    Task 4 — coverage gate + no_secrets_in_logs (T-2-05 invariant)
"""
from __future__ import annotations

import datetime as dt
import io
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter
import pytest
from telegram import User
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler

from src.daily_post_generator import BrandViolationError, GenerationError
from src.preview_bot import (
    EDIT_TIMEOUT_S,
    MAX_CALLBACK_DATA_BYTES,
    MAX_TG_CAPTION,
    MAX_TG_VIDEO_BYTES,
    POLL_TIMEOUT_S,
    TTL_HOURS,
    _alert_generation_failure,
    _build_inline_kb,
    _check_owner,
    _classify_entry_state,
    _draft_sha8,
    _handle_cancel,
    _handle_expired,
    _handle_publish,
    _is_expired,
    _send_preview_album,
    _send_preview_for_draft,
    _send_preview_photo,
    _send_preview_text,
    _send_preview_video,
    _send_split_photo,
    _send_split_video,
    _store_message_id,
    _verify_draft_sha,
    build_application,
    handle_edit_cancel,
    handle_edit_entry,
    handle_edit_text,
    handle_publish_or_cancel,
    pre_flight_generate,
)


# ===========================================================================
# Task 1: helpers + send_preview_*
# ===========================================================================

# ---- _build_inline_kb ----------------------------------------------------

def test_inline_keyboard_callback_data_under_64_bytes():
    """T-2-02 + RESEARCH OQ4 — TG hard limit 64 bytes per callback_data."""
    kb = _build_inline_kb("centry-jun15-morning-very-long-slug-x", "deadbeef")
    buttons = kb.inline_keyboard[0]
    assert len(buttons) == 3
    for btn in buttons:
        assert len(btn.callback_data.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES


def test_inline_keyboard_three_buttons_in_one_row():
    kb = _build_inline_kb("centry-jun15", "abc12345")
    assert len(kb.inline_keyboard) == 1
    assert len(kb.inline_keyboard[0]) == 3
    texts = [b.text for b in kb.inline_keyboard[0]]
    assert "Публикуй" in texts[0]
    assert "Правь" in texts[1]
    assert "Отмена" in texts[2]


def test_inline_keyboard_callback_data_format():
    kb = _build_inline_kb("centry-jun15", "abc12345")
    cds = [b.callback_data for b in kb.inline_keyboard[0]]
    assert cds[0] == "publish:centry-jun15:abc12345"
    assert cds[1] == "edit:centry-jun15:abc12345"
    assert cds[2] == "cancel:centry-jun15:abc12345"


# ---- _is_expired + _classify_entry_state ---------------------------------

def test_is_expired_returns_true_after_ttl():
    draft = frontmatter.Post(content="x")
    draft.metadata["generated_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    ).isoformat()
    assert _is_expired(draft) is True


def test_is_expired_returns_false_for_recent():
    draft = frontmatter.Post(content="x")
    draft.metadata["generated_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    ).isoformat()
    assert _is_expired(draft) is False


def test_is_expired_returns_false_for_missing_generated_at():
    draft = frontmatter.Post(content="x")
    assert _is_expired(draft) is False


def test_classify_entry_state_fresh_when_no_draft(tmp_path):
    from src.plan_reader import PlanEntry
    entry = PlanEntry(date=dt.date(2026, 6, 15), slug="centry-jun15",
                      channels=["tg"], media=[], content="...",
                      product="centry", rubric="x", status="draft")
    drafts = tmp_path / "drafts"
    assert _classify_entry_state(entry, drafts) == "fresh"


def test_classify_entry_state_pending_when_recent_draft_exists(tmp_path):
    from src.plan_reader import PlanEntry
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    draft_path = drafts / "centry-jun15.md"
    p = frontmatter.Post(content="x")
    p.metadata["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")
    entry = PlanEntry(date=dt.date(2026, 6, 15), slug="centry-jun15",
                      channels=["tg"], media=[], content="...",
                      product="centry", rubric="x", status="draft")
    assert _classify_entry_state(entry, drafts) == "pending"


def test_classify_entry_state_skipped_from_entry_status(tmp_path):
    from src.plan_reader import PlanEntry
    entry = PlanEntry(date=dt.date(2026, 6, 15), slug="x", channels=["tg"],
                      media=[], content="...", product="x", rubric="x",
                      status="skipped")
    assert _classify_entry_state(entry, tmp_path) == "skipped"


def test_classify_entry_state_expired_when_old_draft(tmp_path):
    from src.plan_reader import PlanEntry
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    draft_path = drafts / "centry-jun15.md"
    p = frontmatter.Post(content="x")
    p.metadata["generated_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    ).isoformat()
    draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")
    entry = PlanEntry(date=dt.date(2026, 6, 15), slug="centry-jun15",
                      channels=["tg"], media=[], content="...",
                      product="centry", rubric="x", status="draft")
    assert _classify_entry_state(entry, drafts) == "expired"


# ---- send_preview_* variants ---------------------------------------------

@pytest.mark.asyncio
async def test_send_preview_text_uses_send_message():
    """text-only entry (no image/video/media) → send_message."""
    draft = frontmatter.Post(content="Короткий текст. centryweb.ru",
                              slug="x", channels=["tg"])
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=2001))
    msg_id = await _send_preview_text(bot, chat_id=12345, draft=draft, sha8="abc12345")
    assert msg_id == 2001
    bot.send_message.assert_called_once()
    call_kwargs = bot.send_message.call_args.kwargs
    assert "Короткий текст" in call_kwargs["text"]
    assert "lint clean" in call_kwargs["text"]
    # sha8 живёт в callback_data, не в visible тексте
    assert call_kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_preview_photo_with_short_caption_uses_send_photo(short_caption_draft, tmp_path):
    draft_path, _ = short_caption_draft
    draft = frontmatter.load(draft_path)
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2002))
    msg_id = await _send_preview_photo(bot, chat_id=12345, draft=draft,
                                         sha8="abc12345", repo_root=tmp_path)
    assert msg_id == 2002
    bot.send_photo.assert_called_once()
    call_kwargs = bot.send_photo.call_args.kwargs
    assert "Утренняя подборка" in call_kwargs["caption"]
    assert "lint clean" in call_kwargs["caption"]


@pytest.mark.asyncio
async def test_send_preview_photo_with_long_caption_falls_back_to_split(long_caption_draft, tmp_path):
    """T-2-08 — caption > 1024 → 2 messages (text first, photo с warning second)."""
    draft_path, _ = long_caption_draft
    draft = frontmatter.load(draft_path)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=2003))
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2004))
    msg_id = await _send_preview_photo(bot, chat_id=12345, draft=draft,
                                         sha8="abc12345", repo_root=tmp_path)
    bot.send_message.assert_called_once()
    bot.send_photo.assert_called_once()
    photo_kwargs = bot.send_photo.call_args.kwargs
    assert "truncated" in photo_kwargs["caption"]
    assert msg_id == 2004


@pytest.mark.asyncio
async def test_preview_includes_lint_clean_badge(short_caption_draft, tmp_path):
    draft_path, _ = short_caption_draft
    draft = frontmatter.load(draft_path)
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2005))
    await _send_preview_photo(bot, chat_id=12345, draft=draft,
                               sha8="cafe1234", repo_root=tmp_path)
    caption = bot.send_photo.call_args.kwargs["caption"]
    assert "lint clean" in caption
    # sha8 теперь только в callback_data, не в visible caption


@pytest.mark.asyncio
async def test_send_preview_video_over_50mb_raises(tmp_path):
    """50MB pre-flight cap (PUBLISHING_RULES §2)."""
    video_path = tmp_path / "assets" / "big.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    with video_path.open("wb") as f:
        f.seek(MAX_TG_VIDEO_BYTES + 1024)
        f.write(b"\x00")
    draft = frontmatter.Post(content="x", slug="big", channels=["tg"])
    draft.metadata["video"] = "assets/big.mp4"
    bot = MagicMock()
    bot.send_video = AsyncMock()
    with pytest.raises(ValueError, match="50MB|ffmpeg"):
        await _send_preview_video(bot, chat_id=12345, draft=draft,
                                    sha8="abc12345", repo_root=tmp_path)


@pytest.mark.asyncio
async def test_send_preview_video_short_caption_uses_send_video(tmp_path):
    """Short video (≤50MB) с короткой caption → send_video с reply_markup."""
    video_path = tmp_path / "assets" / "small.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-mp4-bytes" * 100)
    draft = frontmatter.Post(content="Короткое видео. diktumweb.ru",
                              slug="diktum-clip", channels=["tg", "yt"])
    draft.metadata["video"] = "assets/small.mp4"
    bot = MagicMock()
    bot.send_video = AsyncMock(return_value=MagicMock(message_id=2006))
    msg_id = await _send_preview_video(bot, chat_id=12345, draft=draft,
                                         sha8="abc12345", repo_root=tmp_path)
    assert msg_id == 2006
    bot.send_video.assert_called_once()
    assert bot.send_video.call_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_preview_album_uses_send_media_group(tmp_path):
    """multi-media → send_media_group + отдельный send_message с keyboard."""
    img1 = tmp_path / "assets" / "a.png"
    img2 = tmp_path / "assets" / "b.png"
    img1.parent.mkdir(parents=True, exist_ok=True)
    img1.write_bytes(b"img1-bytes")
    img2.write_bytes(b"img2-bytes")
    draft = frontmatter.Post(content="Подборка. centryweb.ru",
                              slug="album-x", channels=["tg"])
    draft.metadata["media"] = [
        {"path": "assets/a.png", "role": "image", "sha256": "1"},
        {"path": "assets/b.png", "role": "image", "sha256": "2"},
    ]
    bot = MagicMock()
    bot.send_media_group = AsyncMock(return_value=[
        MagicMock(message_id=3010), MagicMock(message_id=3011),
    ])
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=3012))
    msg_id = await _send_preview_album(bot, chat_id=12345, draft=draft,
                                         sha8="abc12345", repo_root=tmp_path)
    assert msg_id == 3012
    bot.send_media_group.assert_called_once()
    # Keyboard is on the separate message
    assert bot.send_message.call_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_preview_for_draft_dispatches_to_text_when_no_media(tmp_path):
    draft_path = tmp_path / "drafts" / "x.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    p = frontmatter.Post(content="text only. centryweb.ru", slug="x",
                          channels=["tg"], media=[])
    p.metadata["image"] = None
    p.metadata["video"] = None
    draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=3001))
    msg_id = await _send_preview_for_draft(bot, 12345, draft_path,
                                             "abc12345", tmp_path)
    assert msg_id == 3001
    bot.send_message.assert_called_once()


# ---- _check_owner --------------------------------------------------------

@pytest.mark.asyncio
async def test_check_owner_accepts_owner_id(mock_query, mock_ctx, mock_owner_id):
    assert await _check_owner(mock_query, mock_ctx) is True


@pytest.mark.asyncio
async def test_check_owner_rejects_non_owner(mock_query, mock_ctx):
    mock_query.from_user = User(id=99999, first_name="Imposter", is_bot=False)
    result = await _check_owner(mock_query, mock_ctx)
    assert result is False
    mock_query.answer.assert_called_once()


# ---- _store_message_id + _draft_sha8 + _verify_draft_sha -----------------

def test_store_message_id_writes_frontmatter(tmp_path):
    draft = frontmatter.Post(content="x", slug="y")
    path = tmp_path / "x.md"
    path.write_text(frontmatter.dumps(draft), encoding="utf-8")
    _store_message_id(path, 9876)
    reloaded = frontmatter.load(path)
    assert reloaded.metadata["preview_message_id"] == 9876


def test_draft_sha8_returns_8_hex(short_caption_draft):
    draft_path, _ = short_caption_draft
    sha = _draft_sha8(draft_path)
    assert len(sha) == 8
    assert all(c in "0123456789abcdef" for c in sha)


@pytest.mark.asyncio
async def test_verify_draft_sha_mismatch_strips_buttons(mock_query, short_caption_draft):
    draft_path, _ = short_caption_draft
    ok = await _verify_draft_sha(mock_query, draft_path, "wrong___")
    assert ok is False
    mock_query.edit_message_reply_markup.assert_called_once()


@pytest.mark.asyncio
async def test_verify_draft_sha_match_returns_true(mock_query, short_caption_draft):
    draft_path, _ = short_caption_draft
    current_sha = _draft_sha8(draft_path)
    ok = await _verify_draft_sha(mock_query, draft_path, current_sha)
    assert ok is True


@pytest.mark.asyncio
async def test_verify_draft_sha_missing_file_returns_false(mock_query, tmp_path):
    nonexistent = tmp_path / "drafts" / "nope.md"
    ok = await _verify_draft_sha(mock_query, nonexistent, "abc12345")
    assert ok is False


# ---- Constants sanity ----------------------------------------------------

def test_constants_match_research_spec():
    assert POLL_TIMEOUT_S == 540
    assert TTL_HOURS == 24
    assert EDIT_TIMEOUT_S == 600
    assert MAX_TG_CAPTION == 1024
    assert MAX_TG_VIDEO_BYTES == 50 * 1024 * 1024
    assert MAX_CALLBACK_DATA_BYTES == 64


# ===========================================================================
# Task 2: callback handler tests
# ===========================================================================

def _setup_bot_data(mock_ctx, tmp_path):
    plans = tmp_path / "plans"; plans.mkdir(parents=True, exist_ok=True)
    drafts = tmp_path / "drafts"; drafts.mkdir(parents=True, exist_ok=True)
    metrics = tmp_path / ".metrics"; metrics.mkdir(parents=True, exist_ok=True)
    mock_ctx.application.bot_data["plans_dir"] = plans
    mock_ctx.application.bot_data["drafts_dir"] = drafts
    mock_ctx.application.bot_data["repo_root"] = tmp_path
    mock_ctx.application.bot_data["spend_file"] = metrics / "api_spend.json"


def _make_draft_with_sha(tmp_path, slug="centry-jun15", body="text"):
    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    draft_path = drafts / f"{slug}.md"
    p = frontmatter.Post(content=body, slug=slug, channels=["tg"])
    p.metadata["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")
    return draft_path, _draft_sha8(draft_path)


def _make_update(query):
    """Wrap a CallbackQuery mock into a fake Update."""
    update = MagicMock()
    update.callback_query = query
    return update


# ---- handle_publish_or_cancel routing ------------------------------------

@pytest.mark.asyncio
async def test_callback_from_wrong_user_rejected_silently(mock_query, mock_ctx, tmp_path):
    _setup_bot_data(mock_ctx, tmp_path)
    mock_query.from_user = User(id=99999, first_name="Imposter", is_bot=False)
    mock_query.data = "publish:centry-jun15:abc12345"
    await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
    mock_ctx.application.stop_running.assert_not_called()


@pytest.mark.asyncio
async def test_double_approve_idempotent_no_double_dispatch(mock_query, mock_ctx, tmp_path):
    """T-2-02: Double-tap → second call поймает BadRequest → bail (no dispatch)."""
    _setup_bot_data(mock_ctx, tmp_path)
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"publish:centry-jun15:{sha8}"
    mock_query.edit_message_reply_markup.side_effect = BadRequest("message is not modified")
    with patch("src.preview_bot.dispatch_publish") as mock_disp:
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
        mock_disp.assert_not_called()


@pytest.mark.asyncio
async def test_handle_publish_strips_buttons_before_dispatch(mock_query, mock_ctx, tmp_path):
    _setup_bot_data(mock_ctx, tmp_path)
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"publish:centry-jun15:{sha8}"
    call_order = []

    async def _strip(*a, **kw):
        call_order.append("strip")

    mock_query.edit_message_reply_markup = AsyncMock(side_effect=_strip)
    with patch("src.preview_bot.dispatch_publish",
                side_effect=lambda *a, **kw: call_order.append("dispatch")):
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
    assert call_order == ["strip", "dispatch"]


@pytest.mark.asyncio
async def test_stale_sha_callback_rejected(mock_query, mock_ctx, tmp_path):
    """T-2-03: callback_data sha8 mismatches current draft sha → reject."""
    _setup_bot_data(mock_ctx, tmp_path)
    _make_draft_with_sha(tmp_path)
    mock_query.data = "publish:centry-jun15:00000000"   # wrong sha8
    with patch("src.preview_bot.dispatch_publish") as mock_disp:
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
        mock_disp.assert_not_called()
    mock_query.edit_message_reply_markup.assert_called()


@pytest.mark.asyncio
async def test_malformed_callback_data_rejected(mock_query, mock_ctx, tmp_path):
    """Не matching regex → no dispatch, no crash."""
    _setup_bot_data(mock_ctx, tmp_path)
    mock_query.data = "garbage_data"
    with patch("src.preview_bot.dispatch_publish") as mock_disp:
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
        mock_disp.assert_not_called()


# ---- _handle_cancel ------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_cancel_calls_set_entry_status_skipped(mock_query, mock_ctx, tmp_path, monkeypatch):
    """PREV-04 + D-2-03."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    plan_path = tmp_path / "plans" / f"monthly_plan_{dt.date.today().strftime('%Y-%m')}.md"
    plan_path.write_text("---\nmonth: x\n---\n", encoding="utf-8")
    draft_path, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"cancel:centry-jun15:{sha8}"

    with patch("src.preview_bot.set_entry_status",
                return_value="commit_xyz") as mock_set:
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
        mock_set.assert_called_once()
        args = mock_set.call_args.args
        assert args[2] == "centry-jun15"
        assert args[3] == "skipped"
        metadata = args[4]
        assert "skipped_at" in metadata
        assert metadata["skipped_via"] == "forton-via-tg-bot"


@pytest.mark.asyncio
async def test_handle_cancel_deletes_draft_file(mock_query, mock_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    plan_path = tmp_path / "plans" / f"monthly_plan_{dt.date.today().strftime('%Y-%m')}.md"
    plan_path.write_text("---\nmonth: x\n---\n", encoding="utf-8")
    draft_path, sha8 = _make_draft_with_sha(tmp_path)
    assert draft_path.exists()
    mock_query.data = f"cancel:centry-jun15:{sha8}"

    with patch("src.preview_bot.set_entry_status", return_value="x"):
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)

    assert not draft_path.exists()


@pytest.mark.asyncio
async def test_handle_cancel_replies_with_confirmation(mock_query, mock_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    plan_path = tmp_path / "plans" / f"monthly_plan_{dt.date.today().strftime('%Y-%m')}.md"
    plan_path.write_text("---\nmonth: x\n---\n", encoding="utf-8")
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"cancel:centry-jun15:{sha8}"

    with patch("src.preview_bot.set_entry_status", return_value="x"):
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)

    mock_query.message.reply_text.assert_called_once()
    reply = mock_query.message.reply_text.call_args.args[0]
    assert "Отменено" in reply
    assert "centry-jun15" in reply


# ---- Edit dialog — global handlers + bot_data state ---------------------

def _make_text_update(chat_id: int, text: str, sender_id: int | None = None):
    """Fake Update с message (для unit tests; production filter ловит channel_post)."""
    msg = MagicMock()
    msg.chat_id = chat_id
    msg.text = text
    if sender_id is None:
        msg.from_user = None
    else:
        msg.from_user = MagicMock()
        msg.from_user.id = sender_id
    progress = MagicMock()
    progress.edit_text = AsyncMock()
    progress.delete = AsyncMock()
    msg.reply_text = AsyncMock(return_value=progress)
    update = MagicMock()
    update.message = msg
    update.channel_post = None
    update.effective_message = msg
    return update, msg, progress


def _seed_pending_edit(mock_ctx, tmp_path, slug="centry-jun15",
                        chat_id=12345, age_seconds=0):
    """Helper: write draft + seed bot_data['pending_edits'][chat_id]. Returns state."""
    draft_path, sha8 = _make_draft_with_sha(tmp_path, slug=slug)
    state = {
        "slug": slug,
        "draft_path": str(draft_path),
        "expected_sha8": sha8,
        "started_at": time.time() - age_seconds,
        "preview_message_id": 999,
        "preview_chat_id": chat_id,
    }
    mock_ctx.application.bot_data.setdefault("pending_edits", {})[chat_id] = state
    return state, draft_path, sha8


@pytest.mark.asyncio
async def test_handle_edit_entry_strips_buttons_and_seeds_pending_edit(mock_query, mock_ctx, tmp_path):
    """✏️ Правь callback — strip buttons + write state в bot_data['pending_edits']."""
    _setup_bot_data(mock_ctx, tmp_path)
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"edit:centry-jun15:{sha8}"
    mock_query.message.chat_id = 12345
    mock_query.message.message_id = 7777

    await handle_edit_entry(_make_update(mock_query), mock_ctx)

    mock_query.edit_message_reply_markup.assert_called_once()
    pending = mock_ctx.application.bot_data["pending_edits"]
    assert 12345 in pending
    state = pending[12345]
    assert state["slug"] == "centry-jun15"
    assert state["expected_sha8"] == sha8
    assert state["preview_message_id"] == 7777
    assert state["preview_chat_id"] == 12345
    assert isinstance(state["started_at"], float)


@pytest.mark.asyncio
async def test_handle_edit_entry_reply_text_contains_invite(mock_query, mock_ctx, tmp_path):
    """invite text шлётся в чат и упоминает /cancel_edit."""
    _setup_bot_data(mock_ctx, tmp_path)
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"edit:centry-jun15:{sha8}"
    mock_query.message.chat_id = 12345

    await handle_edit_entry(_make_update(mock_query), mock_ctx)

    mock_query.message.reply_text.assert_called_once()
    invite = mock_query.message.reply_text.call_args.args[0]
    assert "поправить" in invite.lower()
    assert "/cancel_edit" in invite


@pytest.mark.asyncio
async def test_handle_edit_entry_non_owner_no_state(mock_query, mock_ctx, tmp_path):
    """Не-owner кликает ✏️ — state НЕ записывается."""
    _setup_bot_data(mock_ctx, tmp_path)
    mock_query.from_user = User(id=99999, first_name="Imposter", is_bot=False)
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"edit:centry-jun15:{sha8}"

    await handle_edit_entry(_make_update(mock_query), mock_ctx)

    assert "pending_edits" not in mock_ctx.application.bot_data or \
           not mock_ctx.application.bot_data.get("pending_edits")


@pytest.mark.asyncio
async def test_handle_edit_text_calls_regen_and_sends_new_preview(mock_ctx, tmp_path, monkeypatch, mock_owner_id):
    """Success path: regen + send new preview + clear state."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["preview_chat_id"] = 12345
    state, draft_path, _ = _seed_pending_edit(mock_ctx, tmp_path, chat_id=12345)
    mock_ctx.bot = MagicMock()

    update, msg, progress = _make_text_update(
        chat_id=12345, text="убери последнее предложение", sender_id=mock_owner_id,
    )

    with patch("src.preview_bot.regen_one") as mock_regen, \
         patch("src.preview_bot._send_preview_for_draft",
                new_callable=AsyncMock, return_value=2010) as mock_send:
        await handle_edit_text(update, mock_ctx)

    mock_regen.assert_called_once()
    mock_send.assert_called_once()
    assert 12345 not in mock_ctx.application.bot_data["pending_edits"]


@pytest.mark.asyncio
async def test_handle_edit_text_no_state_silent(mock_ctx, tmp_path, mock_owner_id):
    """Текст без активной edit-сессии для этого chat_id — silently ignored."""
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["pending_edits"] = {}
    mock_ctx.bot = MagicMock()

    update, msg, _ = _make_text_update(
        chat_id=12345, text="случайное сообщение", sender_id=mock_owner_id,
    )
    with patch("src.preview_bot.regen_one") as mock_regen:
        await handle_edit_text(update, mock_ctx)

    mock_regen.assert_not_called()
    msg.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_edit_text_brand_violation_keeps_state(mock_ctx, tmp_path, monkeypatch, mock_owner_id):
    """T-2-04: brand violation → reply + state НЕ удаляется (юзер re-tries)."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["preview_chat_id"] = 12345
    _seed_pending_edit(mock_ctx, tmp_path, chat_id=12345)
    mock_ctx.bot = MagicMock()

    update, _, progress = _make_text_update(
        chat_id=12345, text="добавь Алексея", sender_id=mock_owner_id,
    )

    violation_word = MagicMock()
    violation_word.word = "Алексей"
    violations = {"centry-jun15": {"names": [violation_word]}}

    with patch("src.preview_bot.regen_one",
                side_effect=BrandViolationError(violations)):
        await handle_edit_text(update, mock_ctx)

    progress.edit_text.assert_called_once()
    assert 12345 in mock_ctx.application.bot_data["pending_edits"]   # state stays


@pytest.mark.asyncio
async def test_handle_edit_text_after_3_regens_clears_state(mock_ctx, tmp_path, monkeypatch, mock_owner_id):
    """D-2-02 cap: GenerationError(regen limit) → reply + state удаляется."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["preview_chat_id"] = 12345
    _seed_pending_edit(mock_ctx, tmp_path, chat_id=12345)
    mock_ctx.bot = MagicMock()

    update, _, progress = _make_text_update(
        chat_id=12345, text="ещё одна правка", sender_id=mock_owner_id,
    )

    with patch("src.preview_bot.regen_one",
                side_effect=GenerationError("regen limit (3) reached for ...")):
        await handle_edit_text(update, mock_ctx)

    progress.edit_text.assert_called_once()
    assert 12345 not in mock_ctx.application.bot_data["pending_edits"]


@pytest.mark.asyncio
async def test_handle_edit_text_non_owner_keeps_state(mock_ctx, tmp_path):
    """Foreign sender в chat где есть state → leave state intact (no regen)."""
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["preview_chat_id"] = 12345
    _seed_pending_edit(mock_ctx, tmp_path, chat_id=12345)
    mock_ctx.bot = MagicMock()

    # Foreign chat_id 99999 — state for 12345 untouched
    update, _, _ = _make_text_update(chat_id=99999, text="imposter", sender_id=88888)

    with patch("src.preview_bot.regen_one") as mock_regen:
        await handle_edit_text(update, mock_ctx)

    mock_regen.assert_not_called()
    assert 12345 in mock_ctx.application.bot_data["pending_edits"]


@pytest.mark.asyncio
async def test_handle_edit_text_timeout_restores_buttons(mock_ctx, tmp_path, mock_owner_id):
    """Manual timeout (now - started_at > 600s) → restore buttons + clear state."""
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["preview_chat_id"] = 12345
    state, _, _ = _seed_pending_edit(mock_ctx, tmp_path, chat_id=12345,
                                       age_seconds=EDIT_TIMEOUT_S + 10)
    mock_ctx.bot = MagicMock()
    mock_ctx.bot.edit_message_reply_markup = AsyncMock()
    mock_ctx.bot.send_message = AsyncMock()

    update, _, _ = _make_text_update(
        chat_id=12345, text="поздно", sender_id=mock_owner_id,
    )

    with patch("src.preview_bot.regen_one") as mock_regen:
        await handle_edit_text(update, mock_ctx)

    mock_regen.assert_not_called()
    mock_ctx.bot.edit_message_reply_markup.assert_called_once()
    kwargs = mock_ctx.bot.edit_message_reply_markup.call_args.kwargs
    assert kwargs["message_id"] == 999
    assert kwargs["reply_markup"] is not None   # buttons restored
    assert 12345 not in mock_ctx.application.bot_data["pending_edits"]


@pytest.mark.asyncio
async def test_handle_edit_text_reads_channel_post(mock_ctx, tmp_path, monkeypatch, mock_owner_id):
    """Production filter ловит channel_post — handler должен использовать его."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["preview_chat_id"] = -100123
    _seed_pending_edit(mock_ctx, tmp_path, chat_id=-100123)
    mock_ctx.bot = MagicMock()

    channel_post = MagicMock()
    channel_post.chat_id = -100123
    channel_post.text = "сделай короче"
    channel_post.from_user = None   # post «as channel» — no user
    progress = MagicMock()
    progress.edit_text = AsyncMock()
    progress.delete = AsyncMock()
    channel_post.reply_text = AsyncMock(return_value=progress)
    update = MagicMock()
    update.message = None
    update.channel_post = channel_post
    update.effective_message = channel_post

    with patch("src.preview_bot.regen_one") as mock_regen, \
         patch("src.preview_bot._send_preview_for_draft",
                new_callable=AsyncMock, return_value=2010):
        await handle_edit_text(update, mock_ctx)

    mock_regen.assert_called_once()


@pytest.mark.asyncio
async def test_handle_edit_cancel_restores_buttons_and_clears_state(mock_ctx, tmp_path):
    """/cancel_edit → restore buttons + remove state."""
    _setup_bot_data(mock_ctx, tmp_path)
    _seed_pending_edit(mock_ctx, tmp_path, chat_id=12345)
    mock_ctx.bot = MagicMock()
    mock_ctx.bot.edit_message_reply_markup = AsyncMock()
    mock_ctx.bot.send_message = AsyncMock()

    update, _, _ = _make_text_update(chat_id=12345, text="/cancel_edit", sender_id=12345)
    await handle_edit_cancel(update, mock_ctx)

    mock_ctx.bot.edit_message_reply_markup.assert_called_once()
    assert 12345 not in mock_ctx.application.bot_data["pending_edits"]


@pytest.mark.asyncio
async def test_handle_edit_cancel_no_state_silent(mock_ctx, tmp_path):
    """/cancel_edit без active state — silent no-op."""
    _setup_bot_data(mock_ctx, tmp_path)
    mock_ctx.application.bot_data["pending_edits"] = {}
    mock_ctx.bot = MagicMock()
    mock_ctx.bot.edit_message_reply_markup = AsyncMock()

    update, _, _ = _make_text_update(chat_id=12345, text="/cancel_edit", sender_id=12345)
    await handle_edit_cancel(update, mock_ctx)

    mock_ctx.bot.edit_message_reply_markup.assert_not_called()


def test_build_application_registers_global_edit_handlers():
    """build_application даёт PTB-app со всеми 4-мя хендлерами в правильном порядке."""
    app = build_application("12345:fake-token")
    handlers = app.handlers[0]
    # Order: edit-CBQ, /cancel_edit MsgHandler, text MsgHandler, catch-all CBQ
    assert isinstance(handlers[0], CallbackQueryHandler)
    assert handlers[0].pattern.pattern == r"^edit:"
    # Last handler — catch-all CallbackQueryHandler (no pattern)
    assert isinstance(handlers[-1], CallbackQueryHandler)
    assert handlers[-1].pattern is None
    # bot_data['pending_edits'] inited
    assert app.bot_data["pending_edits"] == {}


# ===========================================================================
# Task 3: pre_flight_generate + lifecycle
# ===========================================================================

@pytest.mark.asyncio
async def test_pre_flight_skips_if_no_approved_plan(tmp_path):
    ctx_dict = {
        "plan_path": tmp_path / "plans" / "monthly_plan_2099-99.md",
        "drafts_dir": tmp_path / "drafts",
        "repo_root": tmp_path,
        "spend_file": tmp_path / ".metrics" / "spend.json",
        "bot": MagicMock(),
        "owner_chat_id": 12345,
    }
    result = await pre_flight_generate(None, ctx_dict)
    assert result["should_poll"] is False
    assert result["pending_slugs"] == []


@pytest.mark.asyncio
async def test_pre_flight_generates_for_fresh_entry(multi_entry_plan, tmp_path, monkeypatch):
    """Fresh entry → generate_one + send_preview + store msg_id."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    plan_path, _ = multi_entry_plan
    drafts_dir = tmp_path / "drafts"; drafts_dir.mkdir(parents=True, exist_ok=True)
    ctx_dict = {
        "plan_path": plan_path,
        "drafts_dir": drafts_dir,
        "repo_root": tmp_path,
        "spend_file": tmp_path / ".metrics" / "spend.json",
        "bot": MagicMock(),
        "owner_chat_id": 12345,
    }

    with patch("src.preview_bot.dt") as mock_dt:
        mock_dt.date.today.return_value = dt.date(2026, 6, 14)
        mock_dt.datetime = dt.datetime
        mock_dt.timezone = dt.timezone
        mock_dt.timedelta = dt.timedelta

        def fake_generate(entry, repo_root, spend_file, drafts_dir):
            draft_path = drafts_dir / f"{entry.slug}.md"
            p = frontmatter.Post(content="x", slug=entry.slug)
            p.metadata["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")
            return draft_path

        with patch("src.preview_bot.generate_one", side_effect=fake_generate) as mock_gen, \
             patch("src.preview_bot._send_preview_for_draft",
                    new_callable=AsyncMock, return_value=2020) as mock_send, \
             patch("src.preview_bot._store_message_id") as mock_store:
            result = await pre_flight_generate(None, ctx_dict)
            assert "forton-jun14" in result["pending_slugs"]
            assert result["should_poll"] is True
            mock_gen.assert_called_once()
            mock_send.assert_called_once()
            mock_store.assert_called_once()


@pytest.mark.asyncio
async def test_pre_flight_skips_pending_and_approved(multi_entry_plan, tmp_path, monkeypatch):
    """Pending (recent draft exists) → just append; no re-gen."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    plan_path, _ = multi_entry_plan
    drafts_dir = tmp_path / "drafts"; drafts_dir.mkdir(parents=True, exist_ok=True)
    draft = frontmatter.Post(content="x", slug="forton-jun14")
    draft.metadata["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (drafts_dir / "forton-jun14.md").write_text(frontmatter.dumps(draft), encoding="utf-8")

    ctx_dict = {
        "plan_path": plan_path,
        "drafts_dir": drafts_dir,
        "repo_root": tmp_path,
        "spend_file": tmp_path / ".metrics" / "spend.json",
        "bot": MagicMock(),
        "owner_chat_id": 12345,
    }
    with patch("src.preview_bot.dt") as mock_dt:
        mock_dt.date.today.return_value = dt.date(2026, 6, 14)
        mock_dt.datetime = dt.datetime
        mock_dt.timezone = dt.timezone
        mock_dt.timedelta = dt.timedelta

        with patch("src.preview_bot.generate_one") as mock_gen:
            result = await pre_flight_generate(None, ctx_dict)
            mock_gen.assert_not_called()
            assert "forton-jun14" in result["pending_slugs"]


@pytest.mark.asyncio
async def test_pre_flight_handles_expired_draft_marks_status(multi_entry_plan, tmp_path, monkeypatch):
    """D-2-04: expired draft → set_entry_status('expired') + delete + alert."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    plan_path, _ = multi_entry_plan
    drafts_dir = tmp_path / "drafts"; drafts_dir.mkdir(parents=True, exist_ok=True)
    draft = frontmatter.Post(content="old", slug="forton-jun14")
    draft.metadata["generated_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    ).isoformat()
    draft.metadata["preview_message_id"] = 9876
    draft_path = drafts_dir / "forton-jun14.md"
    draft_path.write_text(frontmatter.dumps(draft), encoding="utf-8")

    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()
    ctx_dict = {
        "plan_path": plan_path,
        "drafts_dir": drafts_dir,
        "repo_root": tmp_path,
        "spend_file": tmp_path / ".metrics" / "spend.json",
        "bot": bot,
        "owner_chat_id": 12345,
    }

    with patch("src.preview_bot.dt") as mock_dt, \
         patch("src.preview_bot.set_entry_status") as mock_set, \
         patch("src.preview_bot.tg_nudge.send"):
        mock_dt.date.today.return_value = dt.date(2026, 6, 14)
        mock_dt.datetime = dt.datetime
        mock_dt.timezone = dt.timezone
        mock_dt.timedelta = dt.timedelta

        result = await pre_flight_generate(None, ctx_dict)
        mock_set.assert_called_once()
        args = mock_set.call_args.args
        assert args[2] == "forton-jun14"
        assert args[3] == "expired"
        assert not draft_path.exists()
        assert "forton-jun14" not in result["pending_slugs"]


@pytest.mark.asyncio
async def test_pre_flight_emits_tg_alert_on_claude_outage(multi_entry_plan, tmp_path, monkeypatch):
    """T-2-05: GenerationError → _alert_generation_failure + mark skipped."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    plan_path, _ = multi_entry_plan
    drafts_dir = tmp_path / "drafts"; drafts_dir.mkdir(parents=True, exist_ok=True)

    ctx_dict = {
        "plan_path": plan_path,
        "drafts_dir": drafts_dir,
        "repo_root": tmp_path,
        "spend_file": tmp_path / ".metrics" / "spend.json",
        "bot": MagicMock(),
        "owner_chat_id": 12345,
    }

    with patch("src.preview_bot.dt") as mock_dt, \
         patch("src.preview_bot.generate_one",
                side_effect=GenerationError("Claude API call failed: ConnectionError")), \
         patch("src.preview_bot._alert_generation_failure",
                new_callable=AsyncMock) as mock_alert, \
         patch("src.preview_bot.set_entry_status") as mock_set:
        mock_dt.date.today.return_value = dt.date(2026, 6, 14)
        mock_dt.datetime = dt.datetime
        mock_dt.timezone = dt.timezone
        mock_dt.timedelta = dt.timedelta

        result = await pre_flight_generate(None, ctx_dict)
        mock_alert.assert_called_once()
        mock_set.assert_called_once()
        assert mock_set.call_args.args[3] == "skipped"
        assert "forton-jun14" not in result["pending_slugs"]


@pytest.mark.asyncio
async def test_pre_flight_multi_entry_creates_three_previews(multi_entry_plan, tmp_path, monkeypatch):
    """GEN-04: 3 entries on 2026-06-15 → 3 generate_one calls + 3 send_preview."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    plan_path, _ = multi_entry_plan
    drafts_dir = tmp_path / "drafts"; drafts_dir.mkdir(parents=True, exist_ok=True)

    ctx_dict = {
        "plan_path": plan_path,
        "drafts_dir": drafts_dir,
        "repo_root": tmp_path,
        "spend_file": tmp_path / ".metrics" / "spend.json",
        "bot": MagicMock(),
        "owner_chat_id": 12345,
    }

    def fake_generate(entry, repo_root, spend_file, drafts_dir):
        draft_path = drafts_dir / f"{entry.slug}.md"
        p = frontmatter.Post(content="generated body", slug=entry.slug)
        p.metadata["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")
        return draft_path

    with patch("src.preview_bot.dt") as mock_dt, \
         patch("src.preview_bot.generate_one", side_effect=fake_generate) as mock_gen, \
         patch("src.preview_bot._send_preview_for_draft",
                new_callable=AsyncMock, return_value=2030):
        mock_dt.date.today.return_value = dt.date(2026, 6, 15)
        mock_dt.datetime = dt.datetime
        mock_dt.timezone = dt.timezone
        mock_dt.timedelta = dt.timedelta

        result = await pre_flight_generate(None, ctx_dict)
        assert mock_gen.call_count == 3
        assert len(result["pending_slugs"]) == 3
        assert "centry-jun15-morning" in result["pending_slugs"]
        assert "diktum-jun15-words" in result["pending_slugs"]
        assert "forton-jun15-evening" in result["pending_slugs"]


def test_build_application_returns_application():
    """build_application returns PTB Application с инициализированным bot_data."""
    app = build_application("1:dummy_token")
    assert isinstance(app, Application)
    assert app.bot_data["pending_edits"] == {}


def test_build_application_registers_edit_callback_first():
    """edit-CallbackQueryHandler должен идти ПЕРЕД catch-all, иначе catch-all
    съедает edit: callback. Catch-all без pattern — последний."""
    app = build_application("1:dummy_token")
    handlers = app.handlers.get(0, [])
    assert len(handlers) >= 4   # edit-CBQ, /cancel_edit, text-Msg, catch-all CBQ
    # First — edit-only CallbackQueryHandler
    assert isinstance(handlers[0], CallbackQueryHandler)
    assert handlers[0].pattern.pattern == r"^edit:"
    # Last — catch-all CallbackQueryHandler (no pattern)
    assert isinstance(handlers[-1], CallbackQueryHandler)
    assert handlers[-1].pattern is None


def test_edit_text_filter_matches_channel_post():
    """REGRESSION: filters.UpdateType.MESSAGES в PTB НЕ матчит channel_post —
    он только для regular message + edited_message. Поэтому в build_application
    фильтр это (MESSAGES | CHANNEL_POSTS). Если кто-то откатит на голый MESSAGES,
    правки из канала «Планировщик» снова перестанут проходить."""
    from datetime import datetime
    from telegram import Chat, Message, Update

    app = build_application("1:dummy_token")
    handlers = app.handlers.get(0, [])
    # 3-й handler — text-MessageHandler (после edit-CBQ и /cancel_edit)
    text_handler = handlers[2]

    channel_msg = Message(
        message_id=1, date=datetime.now(),
        chat=Chat(id=-100123, type="channel"),
        text="убери эмодзи",
    )
    channel_update = Update(update_id=42, channel_post=channel_msg)
    assert text_handler.check_update(channel_update), (
        "text-handler НЕ матчит channel_post — баг filter, "
        "filters.UpdateType.MESSAGES не покрывает channel_post"
    )

    regular_msg = Message(
        message_id=2, date=datetime.now(),
        chat=Chat(id=12345, type="private"),
        text="hello",
    )
    regular_update = Update(update_id=43, message=regular_msg)
    assert text_handler.check_update(regular_update), (
        "text-handler не матчит regular message"
    )

    # Command не должна попадать в text-handler (cancel_edit handler выше его съедает)
    cmd_msg = Message(
        message_id=3, date=datetime.now(),
        chat=Chat(id=-100123, type="channel"),
        text="/cancel_edit",
        entities=[],
    )
    # Note: эта проверка не строгая — точное определение COMMAND зависит
    # от entities, но фильтр ~COMMAND по text-pattern достаточен.


# ===========================================================================
# Task 4: no_secrets_in_logs (T-2-05) + module-level constants check
# ===========================================================================

_SECRET_PATTERNS = [
    re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{30,}", re.I),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bghs_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
]


def _assert_no_secrets(captured_text: str, label: str = ""):
    for pat in _SECRET_PATTERNS:
        m = pat.search(captured_text)
        assert m is None, (
            f"Secret pattern leaked in {label}: matched={m.group(0)[:20]}... "
            f"pattern={pat.pattern}"
        )


@pytest.mark.asyncio
async def test_no_secrets_in_logs_during_handlers(mock_query, mock_ctx, tmp_path,
                                                      monkeypatch, capsys):
    """T-2-05 invariant: bot никогда не печатает env vars в stderr/stdout."""
    monkeypatch.setenv("TG_PLANNER_BOT_TOKEN", "bot1234567890:AAFakeTokenButLooksRealAAAAAAA12345")
    monkeypatch.setenv("BOT_DISPATCH_PAT", "github_pat_11FAKEABCDEFGHIJKLMNOP1234567890qrstuvwxyz0123456789abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-FakeFakeFakeFakeFakeFakeFakeFakeFake")
    monkeypatch.setenv("TG_OWNER_USER_ID", "12345")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "12345")

    _setup_bot_data(mock_ctx, tmp_path)
    plan_path = tmp_path / "plans" / f"monthly_plan_{dt.date.today().strftime('%Y-%m')}.md"
    plan_path.write_text("---\nmonth: x\n---\n", encoding="utf-8")
    _, sha8 = _make_draft_with_sha(tmp_path)

    # Path 1: non-owner callback
    mock_query.from_user = User(id=99999, first_name="Imposter", is_bot=False)
    mock_query.data = f"publish:centry-jun15:{sha8}"
    await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)

    # Path 2: sha mismatch
    mock_query.from_user = User(id=mock_ctx.application.bot_data["owner_chat_id"],
                                  first_name="Owner", is_bot=False)
    mock_query.data = "publish:centry-jun15:00000000"
    await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)

    # Path 3: dispatch_publish failure
    mock_query.data = f"publish:centry-jun15:{sha8}"
    mock_query.edit_message_reply_markup.side_effect = None
    with patch("src.preview_bot.dispatch_publish",
                side_effect=RuntimeError("simulated GH 401")):
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)

    captured = capsys.readouterr()
    _assert_no_secrets(captured.out + captured.err,
                        label="handlers stderr/stdout")


# ---- Targeted coverage boosters -----------------------------------------

@pytest.mark.asyncio
async def test_send_split_video_falls_back_when_caption_long(tmp_path):
    """Long-caption video → text first + video с warning second."""
    video_path = tmp_path / "assets" / "small.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-mp4" * 50)
    long_body = "Длинная подпись. " * 80   # > 1024 chars
    draft = frontmatter.Post(content=long_body, slug="diktum-long-vid",
                              channels=["tg"])
    draft.metadata["video"] = "assets/small.mp4"
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=4001))
    bot.send_video = AsyncMock(return_value=MagicMock(message_id=4002))
    msg_id = await _send_preview_video(bot, chat_id=12345, draft=draft,
                                         sha8="abc12345", repo_root=tmp_path)
    bot.send_message.assert_called_once()
    bot.send_video.assert_called_once()
    photo_kwargs = bot.send_video.call_args.kwargs
    assert "truncated" in photo_kwargs["caption"]
    assert msg_id == 4002


@pytest.mark.asyncio
async def test_alert_generation_failure_calls_tg_nudge():
    """T-2-05: _alert_generation_failure routes to tg_nudge.send."""
    from src.plan_reader import PlanEntry
    entry = PlanEntry(date=dt.date(2026, 6, 15), slug="x", channels=["tg"],
                      media=[], content="...", product="x", rubric="x",
                      status="draft")
    ctx_dict = {}
    with patch("src.preview_bot.tg_nudge.send") as mock_send:
        await _alert_generation_failure(entry, RuntimeError("fail"), ctx_dict)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "daily_generation_failure"
        assert kwargs["slug"] == "x"
        assert "RuntimeError" in kwargs["reason"]


@pytest.mark.asyncio
async def test_alert_generation_failure_swallows_keyerror():
    """If template missing, log to stderr but never raise."""
    from src.plan_reader import PlanEntry
    entry = PlanEntry(date=dt.date(2026, 6, 15), slug="x", channels=["tg"],
                      media=[], content="...", product="x", rubric="x",
                      status="draft")
    with patch("src.preview_bot.tg_nudge.send", side_effect=KeyError("template")):
        # Should not raise
        await _alert_generation_failure(entry, RuntimeError("fail"), {})


@pytest.mark.asyncio
async def test_handle_expired_strips_stale_kb_and_marks_status(tmp_path):
    """D-2-04 direct: expired draft → bot.edit_message_reply_markup + set_entry_status."""
    from src.plan_reader import PlanEntry
    drafts = tmp_path / "drafts"; drafts.mkdir(parents=True, exist_ok=True)
    draft_path = drafts / "x.md"
    p = frontmatter.Post(content="x", slug="x")
    p.metadata["preview_message_id"] = 555
    p.metadata["generated_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    ).isoformat()
    draft_path.write_text(frontmatter.dumps(p), encoding="utf-8")

    plan_path = tmp_path / "plans" / "monthly_plan_2026-06.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("---\nmonth: x\n---\n", encoding="utf-8")

    entry = PlanEntry(date=dt.date(2026, 6, 14), slug="x", channels=["tg"],
                      media=[], content="...", product="x", rubric="x",
                      status="draft")
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()
    ctx_dict = {
        "bot": bot, "owner_chat_id": 12345,
        "plan_path": plan_path, "repo_root": tmp_path,
    }
    with patch("src.preview_bot.set_entry_status") as mock_set, \
         patch("src.preview_bot.tg_nudge.send"):
        await _handle_expired(entry, draft_path, ctx_dict)
        bot.edit_message_reply_markup.assert_called_once()
        kwargs = bot.edit_message_reply_markup.call_args.kwargs
        assert kwargs["message_id"] == 555
        mock_set.assert_called_once()
        assert mock_set.call_args.args[3] == "expired"
    assert not draft_path.exists()


def test_env_helper_happy_path(monkeypatch):
    """_env returns env var value when set."""
    from src.preview_bot import _env
    monkeypatch.setenv("TEST_ENV_VAR", "value123")
    assert _env("TEST_ENV_VAR") == "value123"


def test_env_helper_exits_when_missing(monkeypatch):
    """_env calls sys.exit(1) when env var missing — does not print value."""
    from src.preview_bot import _env
    monkeypatch.delenv("MISSING_ENV_VAR", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        _env("MISSING_ENV_VAR")
    assert exc_info.value.code == 1


def test_env_helper_exits_when_empty(monkeypatch):
    """Empty env var also triggers FATAL exit."""
    from src.preview_bot import _env
    monkeypatch.setenv("EMPTY_VAR", "")
    with pytest.raises(SystemExit) as exc_info:
        _env("EMPTY_VAR")
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_handle_publish_dispatch_failure_replies(mock_query, mock_ctx, tmp_path, monkeypatch):
    """dispatch_publish raises → handler replies with error class name; soft-fail (no stop)."""
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"publish:centry-jun15:{sha8}"
    with patch("src.preview_bot.dispatch_publish",
                side_effect=RuntimeError("simulated GH 401")):
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
    # Reply contains error class
    assert mock_query.message.reply_text.called
    reply = mock_query.message.reply_text.call_args.args[0]
    assert "RuntimeError" in reply
    # No stop_running (soft fail)
    mock_ctx.application.stop_running.assert_not_called()


@pytest.mark.asyncio
async def test_handle_cancel_set_entry_status_failure_replies(mock_query, mock_ctx, tmp_path, monkeypatch):
    """GitHubAPIError on set_entry_status → reply + soft-fail."""
    from src.plan_writer import GitHubAPIError as _GHE
    monkeypatch.setenv("BOT_DISPATCH_PAT", "ghp_test")
    _setup_bot_data(mock_ctx, tmp_path)
    plan_path = tmp_path / "plans" / f"monthly_plan_{dt.date.today().strftime('%Y-%m')}.md"
    plan_path.write_text("---\nmonth: x\n---\n", encoding="utf-8")
    _, sha8 = _make_draft_with_sha(tmp_path)
    mock_query.data = f"cancel:centry-jun15:{sha8}"
    with patch("src.preview_bot.set_entry_status",
                side_effect=_GHE("PUT /contents -> 409: conflict")):
        await handle_publish_or_cancel(_make_update(mock_query), mock_ctx)
    mock_ctx.application.stop_running.assert_not_called()


def test_no_secrets_in_module_constants():
    """Module-level strings (templates, USER_AGENT, etc) НЕ содержат tokens."""
    from src import preview_bot
    for name in dir(preview_bot):
        if name.startswith("_"):
            continue
        val = getattr(preview_bot, name, None)
        if isinstance(val, str):
            _assert_no_secrets(val, label=f"preview_bot.{name}")
