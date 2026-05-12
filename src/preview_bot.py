"""preview_bot — Phase 2 daily preview bot (global handlers, no ConversationHandler).

Architecture (RESEARCH §«Daily Generator Architecture»):
    Cron */10 9-19 UTC (12-22 МСК) triggers preview_bot.yml. main() does:
    1. pre_flight_generate: для каждого today_entry → classify → fresh→generate+send;
       expired→cleanup+alert; pending→keep; approved/skipped→skip.
    2. Если есть pending → build_application + run_polling 9-min window.
    3. Approve/Cancel callback → side-effect → stop_running() → workflow exits.

Edit flow (Phase 2.5, без ConversationHandler):
    PTB ConversationHandler.check_update требует update.effective_user, которого
    нет у channel_post. Поэтому edit-сессия живёт в app.bot_data['pending_edits']
    {chat_id: {slug, draft_path, expected_sha8, started_at,
               preview_message_id, preview_chat_id}}; глобальные хендлеры
    (CallbackQuery edit:, MessageHandler по channel_post, CommandHandler
    /cancel_edit) читают/пишут этот dict, manual timeout check 600 сек.

Threat-model anchors (T-2-01..05, T-2-08):
    T-2-01 — _check_owner is FIRST gate в каждом handler.
    T-2-02 — editMessageReplyMarkup(None) ДО любого side-effect; double-tap → BadRequest → bail.
    T-2-03 — sha8 в callback_data + _verify_draft_sha; TTL 24h pre-flight.
    T-2-04 — brand-lint hard-fail в handle_edit_text → state НЕ удаляется, юзер re-tries.
    T-2-05 — GenerationError catch в pre_flight → tg_nudge alert «Claude outage» → skip entry.
    T-2-08 — long caption (>1024) → _send_split_* fallback с warning.

Public API:
    main, build_application,
    pre_flight_generate, _classify_entry_state, _is_expired,
    _check_owner, _verify_draft_sha, _build_inline_kb, _draft_sha8,
    _send_preview_for_draft, _send_preview_text, _send_preview_photo,
    _send_preview_video, _send_preview_album, _send_split_photo, _send_split_video,
    _store_message_id, _alert_generation_failure,
    handle_publish_or_cancel, handle_edit_entry, handle_edit_text,
    handle_edit_cancel,
    _handle_publish, _handle_cancel, _handle_expired,
    POLL_TIMEOUT_S, TTL_HOURS, EDIT_TIMEOUT_S,
    MAX_TG_CAPTION, MAX_TG_VIDEO_BYTES, MAX_CALLBACK_DATA_BYTES
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path
from typing import Final

import frontmatter
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from src import tg_nudge
from src.daily_post_generator import (
    MAX_REGEN_PER_DRAFT,
    BrandViolationError,
    GenerationError,
    generate_one,
    regen_one,
)
from src.monthly_plan_generator import BudgetExceededError
from src.plan_reader import PlanEntry, get_today_entries, parse_plan
from src.plan_writer import (
    GitHubAPIError,
    dispatch_publish,
    plan_sha8,
    set_entry_status,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_TIMEOUT_S: Final[int] = 540   # 9 min; leaves 1-min headroom before GH 10-min cron tick
TTL_HOURS: Final[int] = 24          # D-2-04 — draft auto-expire window
EDIT_TIMEOUT_S: Final[int] = 600    # D-2-02 — 10-min edit window (manual timeout check)
MAX_TG_CAPTION: Final[int] = 1024   # TG sendPhoto/sendVideo caption byte limit (chars approx)
MAX_TG_VIDEO_BYTES: Final[int] = 50 * 1024 * 1024   # PUBLISHING_RULES §2 sendVideo
MAX_CALLBACK_DATA_BYTES: Final[int] = 64   # TG hard limit
PLANS_DIR_NAME: Final[str] = "plans"
DRAFTS_DIR_NAME: Final[str] = "drafts"
SPEND_FILE_REL: Final[str] = ".metrics/api_spend.json"

_CALLBACK_DATA_RE: Final[re.Pattern] = re.compile(
    r"^(publish|edit|cancel):([a-z0-9-]{1,40}):([a-f0-9]{8})$"
)

def _today_msk() -> dt.date:
    """Сегодняшняя дата по МСК.

    На локальном маке (TZ=МСК) — `dt.date.today()` уже МСК.
    На GH Actions runner (TZ=UTC) — `dt.date.today()` показывает UTC; если
    сейчас ≥21:00 UTC (= ≥00:00 МСК следующего дня) И это manual trigger,
    сдвигаем на +1 день — нужно для workflow_dispatch в 03:00 МСК чтобы
    юзер мог триггерить ночью на «сегодняшний» (МСК) пост.

    КРИТИЧНО: для schedule events hack ВЫКЛЮЧЕН. Incident 2026-05-11
    показал что GH Actions back-fills пропущенный cron-тик может прийти
    в 21:03 UTC → hack сдвигал date → preview прилетал в полночь МСК
    на завтрашнюю запись. Теперь back-fill schedule в 21:00+ UTC видит
    UTC date и exit'ит т.к. вчерашняя запись уже published/skipped.

    Detect runner через GITHUB_ACTIONS env; event type — GITHUB_EVENT_NAME.
    Тесты patch'ат `dt.date.today` — env var-ы не выставляют, hack не сработает.
    """
    today = dt.date.today()
    if (os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"):
        # tz-aware UTC now (Python 3.12+ deprecated utcnow())
        if dt.datetime.now(dt.timezone.utc).hour >= 21:
            today = today + dt.timedelta(days=1)
    return today


class _SoftFail(Exception):
    """Raised by handler to signal NOT to call stop_running (recoverable)."""


# ---------------------------------------------------------------------------
# Owner gate (verbatim reuse pattern from Phase 1.5 monthly_approval_bot)
# ---------------------------------------------------------------------------

async def _check_owner(query, ctx) -> bool:
    """T-2-01 first gate. Silent reject from non-owner."""
    owner = ctx.application.bot_data["owner_chat_id"]
    if query.from_user.id != owner:
        try:
            await query.answer("Not authorized", show_alert=False)
        except Exception:
            pass
        sys.stderr.write(
            f"WARN: callback from non-owner user_id={query.from_user.id} "
            f"(expected {owner})\n"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# sha8 anti-replay (T-2-03)
# ---------------------------------------------------------------------------

def _draft_sha8(draft_path: Path) -> str:
    """Sha8 от draft.content + slug — НЕ от полного файла.

    Раньше использовали `plan_sha8(file)` который sha-шит ВСЕ байты файла,
    включая frontmatter. Это создавало баг: после `_store_message_id` в
    pre_flight_generate (которая дописывает preview_message_id в frontmatter)
    sha файла менялся → callback кнопок становился invalid сразу после
    отправки preview → юзер кликает → mismatch.

    Now: sha8 — это invariant самого контента поста, который regen_one
    меняет (правильно invalidates кнопки старых preview), а
    _store_message_id не трогает (правильно сохраняет валидность).
    """
    import hashlib
    draft = frontmatter.load(draft_path)
    slug = str(draft.metadata.get("slug") or draft_path.stem)
    payload = (draft.content + "\n---\n" + slug).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:8]


async def _verify_draft_sha(query, draft_path: Path, expected_sha8: str) -> bool:
    """Compare current draft sha8 vs expected from callback_data. Mismatch → reject."""
    if not draft_path.exists():
        try:
            await query.edit_message_text(
                f"⚠️ Draft <code>{draft_path.name}</code> отсутствует — "
                f"публикация невозможна",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return False
    current = _draft_sha8(draft_path)
    if current != expected_sha8:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"⚠️ Draft <code>{draft_path.name}</code> изменился (sha mismatch). "
                f"Жди новый preview.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        sys.stderr.write(
            f"WARN: sha mismatch for {draft_path.name}: "
            f"expected={expected_sha8} current={current}\n"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Inline keyboard (D-2-01 + RESEARCH §«Inline keyboard layout»)
# ---------------------------------------------------------------------------

def _build_inline_kb(slug: str, sha8: str) -> InlineKeyboardMarkup:
    """3 кнопки в одном ряду. Защита: callback_data ≤ 64 bytes (TG limit)."""
    safe_slug = slug[:40] if len(slug) > 40 else slug
    publish_data = f"publish:{safe_slug}:{sha8}"
    edit_data = f"edit:{safe_slug}:{sha8}"
    cancel_data = f"cancel:{safe_slug}:{sha8}"
    for cd in (publish_data, edit_data, cancel_data):
        assert len(cd.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES, (
            f"callback_data {cd!r} > {MAX_CALLBACK_DATA_BYTES} bytes (TG hard limit)"
        )
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Публикуй",  callback_data=publish_data),
        InlineKeyboardButton("✏️ Правь",     callback_data=edit_data),
        InlineKeyboardButton("❌ Отмена",     callback_data=cancel_data),
    ]])


# ---------------------------------------------------------------------------
# Draft state classification + lifecycle helpers
# ---------------------------------------------------------------------------

def _is_expired(draft: frontmatter.Post) -> bool:
    """Draft generated > TTL_HOURS hours ago — eligible for expire cleanup."""
    gen_at_str = draft.metadata.get("generated_at")
    if not gen_at_str:
        return False
    try:
        gen_at = dt.datetime.fromisoformat(str(gen_at_str).replace("Z", "+00:00"))
    except ValueError:
        return False
    if gen_at.tzinfo is None:
        gen_at = gen_at.replace(tzinfo=dt.timezone.utc)
    age_hours = (
        dt.datetime.now(dt.timezone.utc) - gen_at
    ).total_seconds() / 3600
    return age_hours > TTL_HOURS


def _classify_entry_state(entry: PlanEntry, drafts_dir: Path) -> str:
    """Return 'fresh' | 'pending' | 'expired' | 'approved' | 'skipped' | 'published'.

    Terminal statuses (skipped/approved/expired/published) — no further action,
    no draft generation. Bot saw 'published' record and tried to re-generate
    until this guard was added.
    """
    if entry.status in ("skipped", "approved", "expired", "published"):
        return entry.status
    draft_path = drafts_dir / f"{entry.slug}.md"
    if not draft_path.exists():
        return "fresh"
    draft = frontmatter.load(draft_path)
    if _is_expired(draft):
        return "expired"
    return "pending"


def _store_message_id(draft_path: Path, message_id: int) -> None:
    """Write `preview_message_id` field в draft frontmatter (для TTL stale-kb cleanup)."""
    draft = frontmatter.load(draft_path)
    draft.metadata["preview_message_id"] = int(message_id)
    draft_path.write_text(frontmatter.dumps(draft), encoding="utf-8")


# ---------------------------------------------------------------------------
# Preview send — 5 variants (D-2-01 WYSIWYG)
# ---------------------------------------------------------------------------

_LINT_BADGE_TPL: Final[str] = "\n\n<i>✓ lint clean</i>"


async def _send_preview_text(bot, chat_id: int, draft: frontmatter.Post,
                              sha8: str) -> int:
    """text-only entry: send_message + inline_keyboard."""
    text = draft.content + _LINT_BADGE_TPL.format(sha8=sha8)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
        reply_markup=_build_inline_kb(draft.metadata["slug"], sha8),
    )
    return msg.message_id


async def _send_preview_photo(bot, chat_id: int, draft: frontmatter.Post,
                               sha8: str, repo_root: Path) -> int:
    """single image: send_photo с caption если ≤MAX_TG_CAPTION; иначе split fallback."""
    image_path = repo_root / draft.metadata["image"]
    body = draft.content
    badge = _LINT_BADGE_TPL.format(sha8=sha8)
    full_caption = body + badge

    if len(full_caption) <= MAX_TG_CAPTION:
        with image_path.open("rb") as f:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_inline_kb(draft.metadata["slug"], sha8),
            )
        return msg.message_id
    return await _send_split_photo(bot, chat_id, image_path, body, badge,
                                    draft.metadata["slug"], sha8)


async def _send_split_photo(bot, chat_id: int, image_path: Path,
                             body: str, badge: str, slug: str, sha8: str) -> int:
    """T-2-08 fallback: full text first message + photo+badge с warning second."""
    await bot.send_message(
        chat_id=chat_id,
        text=body,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    short_caption = (
        f"<i>⚠️ caption truncated в production (preview split в 2 сообщения)</i>"
        f"\n{badge.strip()}"
    )
    with image_path.open("rb") as f:
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=f,
            caption=short_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=_build_inline_kb(slug, sha8),
        )
    return msg.message_id


def _video_meta(video_path: Path) -> dict:
    """ffprobe → {width, height, duration} или пустой dict если ffprobe нет.

    БЕЗ этого Telegram iOS не знает реальные пропорции видео и рендерит
    9:16 как растянутые/обрезанные. Workflow YAML должен apt-install ffmpeg.
    """
    import shutil
    import subprocess
    if shutil.which("ffprobe") is None:
        sys.stderr.write("WARN: ffprobe not found; video meta omitted (iOS will stretch)\n")
        return {}
    try:
        import json as _json
        out = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(video_path),
        ], capture_output=True, timeout=20, check=True, text=True)
        data = _json.loads(out.stdout)
        s = data["streams"][0]
        return {
            "width": int(s["width"]),
            "height": int(s["height"]),
            "duration": int(float(data["format"]["duration"])),
        }
    except Exception as exc:
        sys.stderr.write(f"WARN: ffprobe failed: {exc!r}\n")
        return {}


async def _send_preview_video(bot, chat_id: int, draft: frontmatter.Post,
                               sha8: str, repo_root: Path) -> int:
    """single video: pre-flight 50MB cap, send_video с caption или split."""
    video_path = repo_root / draft.metadata["video"]
    if video_path.stat().st_size > MAX_TG_VIDEO_BYTES:
        raise ValueError(
            f"{video_path.name}: > {MAX_TG_VIDEO_BYTES} bytes "
            f"(50MB); ffmpeg-compress per PUBLISHING_RULES §3"
        )
    body = draft.content
    badge = _LINT_BADGE_TPL.format(sha8=sha8)
    full_caption = body + badge
    meta = _video_meta(video_path)

    if len(full_caption) <= MAX_TG_CAPTION:
        with video_path.open("rb") as f:
            msg = await bot.send_video(
                chat_id=chat_id,
                video=f,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                reply_markup=_build_inline_kb(draft.metadata["slug"], sha8),
                **meta,
            )
        return msg.message_id
    return await _send_split_video(bot, chat_id, video_path, body, badge,
                                    draft.metadata["slug"], sha8)


async def _send_split_video(bot, chat_id: int, video_path: Path,
                             body: str, badge: str, slug: str, sha8: str) -> int:
    await bot.send_message(
        chat_id=chat_id,
        text=body,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    short_caption = (
        f"<i>⚠️ caption truncated в production (preview split в 2 сообщения)</i>"
        f"\n{badge.strip()}"
    )
    meta = _video_meta(video_path)
    with video_path.open("rb") as f:
        msg = await bot.send_video(
            chat_id=chat_id,
            video=f,
            caption=short_caption,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
            reply_markup=_build_inline_kb(slug, sha8),
            **meta,
        )
    return msg.message_id


async def _send_preview_album(bot, chat_id: int, draft: frontmatter.Post,
                               sha8: str, repo_root: Path) -> int:
    """multi-media: send_media_group (caption на FIRST item) + отдельный send_message с keyboard.

    Returns message_id of the keyboard-bearing message (для TTL cleanup).
    """
    media_inputs = []
    open_handles = []
    try:
        for i, m in enumerate(draft.metadata["media"]):
            path = repo_root / m["path"]
            f = path.open("rb")
            open_handles.append(f)
            cls = InputMediaPhoto if m.get("role") == "image" else InputMediaVideo
            caption = draft.content[:MAX_TG_CAPTION] if i == 0 else None
            media_inputs.append(cls(media=f, caption=caption,
                                    parse_mode=ParseMode.HTML))
        msgs = await bot.send_media_group(chat_id=chat_id, media=media_inputs)
    finally:
        for f in open_handles:
            try:
                f.close()
            except Exception:
                pass
    # Album doesn't support reply_markup → keyboard в отдельном message
    kb_msg = await bot.send_message(
        chat_id=chat_id,
        text=_LINT_BADGE_TPL.format(sha8=sha8).strip(),
        parse_mode=ParseMode.HTML,
        reply_to_message_id=msgs[-1].message_id if msgs else None,
        reply_markup=_build_inline_kb(draft.metadata["slug"], sha8),
    )
    return kb_msg.message_id


async def _send_preview_for_draft(bot, chat_id: int, draft_path: Path,
                                   sha8: str, repo_root: Path) -> int:
    """Dispatch на правильный send_preview_* variant based on media. Returns msg_id."""
    draft = frontmatter.load(draft_path)
    media = draft.metadata.get("media") or []
    if len(media) >= 2:
        return await _send_preview_album(bot, chat_id, draft, sha8, repo_root)
    if draft.metadata.get("video"):
        return await _send_preview_video(bot, chat_id, draft, sha8, repo_root)
    if draft.metadata.get("image"):
        return await _send_preview_photo(bot, chat_id, draft, sha8, repo_root)
    return await _send_preview_text(bot, chat_id, draft, sha8)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolve_plan_path(ctx) -> Path:
    """Current month plan path from bot_data."""
    plans_dir = ctx.application.bot_data["plans_dir"]
    today = _today_msk()
    return plans_dir / f"monthly_plan_{today.strftime('%Y-%m')}.md"


def _resolve_draft_path(ctx, slug: str) -> Path:
    drafts_dir = ctx.application.bot_data["drafts_dir"]
    return drafts_dir / f"{slug}.md"


# ---------------------------------------------------------------------------
# Approve/Cancel callback handlers
# ---------------------------------------------------------------------------

async def handle_publish_or_cancel(update: Update,
                                    ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all CallbackQueryHandler для publish: и cancel: patterns.
    Edit: pattern owned by separate CallbackQueryHandler(handle_edit_entry)
    зарегистрированным раньше — этот handler пропускает edit: action."""
    query = update.callback_query
    sys.stderr.write(
        f"INFO: handle_publish_or_cancel fired data={query.data!r} "
        f"from_user={getattr(query.from_user, 'id', None)}\n"
    )
    try:
        await query.answer()
    except BadRequest as exc:
        # «query is too old» если callback пришёл во время длинного edit-flow
        # и устарел в очереди (concurrent_updates=False). Не блокирует side-
        # effect — кнопки и dispatch продолжают, но пользователь увидит, что
        # кнопки уже не отвечают. Если такое часто — switch on concurrent_updates.
        sys.stderr.write(f"WARN: query.answer failed: {exc!r}\n")
    if not await _check_owner(query, ctx):
        return
    m = _CALLBACK_DATA_RE.match(query.data or "")
    if not m:
        sys.stderr.write(f"WARN: malformed callback_data: {query.data!r}\n")
        return
    action, slug, sha8 = m.group(1), m.group(2), m.group(3)
    draft_path = _resolve_draft_path(ctx, slug)
    if not await _verify_draft_sha(query, draft_path, sha8):
        sys.stderr.write(
            f"WARN: sha mismatch action={action} slug={slug} sha8={sha8}\n"
        )
        return
    try:
        if action == "publish":
            await _handle_publish(query, ctx, slug, draft_path)
        elif action == "cancel":
            await _handle_cancel(query, ctx, slug, draft_path)
        else:
            return   # edit handled by separate CallbackQueryHandler
    except _SoftFail as exc:
        sys.stderr.write(f"INFO: soft-fail in {action}: {exc}\n")
        return
    # Stop polling — workflow exits cleanly after first action
    try:
        ctx.application.stop_running()
    except Exception as exc:
        sys.stderr.write(f"WARN: stop_running failed: {exc!r}\n")


async def _handle_publish(query, ctx, slug: str, draft_path: Path) -> None:
    """D-2-06: dispatch publish.yml через GH API. T-2-02 idempotency."""
    # Strip buttons FIRST (idempotency lock — T-2-02)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            sys.stderr.write("INFO: duplicate publish callback (already removed)\n")
            raise _SoftFail("duplicate publish")
        raise

    # Trigger publish.yml workflow
    try:
        await asyncio.to_thread(dispatch_publish, slug)
    except Exception as exc:
        sys.stderr.write(f"ERROR: dispatch_publish failed: {type(exc).__name__}\n")
        try:
            await query.message.reply_text(
                f"❌ Не удалось дёрнуть publish.yml: <code>{type(exc).__name__}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        raise _SoftFail("dispatch_publish failed")

    now_msk = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).strftime("%H:%M")
    await query.message.reply_text(
        f"✅ <b>Публикую в каналы</b>\n"
        f"Slug: <code>{slug}</code>\n"
        f"Время: {now_msk} МСК\n"
        f"Жди подтверждения через ~2-3 мин.",
        parse_mode=ParseMode.HTML,
    )


async def _handle_cancel(query, ctx, slug: str, draft_path: Path) -> None:
    """D-2-03: status: skipped + delete draft + commit. T-2-01."""
    # Strip buttons FIRST
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            sys.stderr.write("INFO: duplicate cancel callback\n")
            raise _SoftFail("duplicate cancel")
        raise

    plan_path = _resolve_plan_path(ctx)
    repo_root = ctx.application.bot_data["repo_root"]
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        await asyncio.to_thread(
            set_entry_status,
            plan_path, repo_root, slug, "skipped",
            {"skipped_at": now_iso, "skipped_via": "forton-via-tg-bot"},
        )
    except GitHubAPIError as exc:
        sys.stderr.write(f"ERROR: set_entry_status failed: {exc}\n")
        try:
            await query.message.reply_text(
                f"⚠️ Не удалось обновить план: <code>{type(exc).__name__}</code>. "
                f"Draft не удалён, попробуй ещё раз.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        raise _SoftFail("set_entry_status failed")

    # Delete local draft
    try:
        draft_path.unlink(missing_ok=True)
    except OSError as exc:
        sys.stderr.write(f"WARN: draft unlink failed: {exc}\n")

    now_msk = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).strftime("%H:%M")
    await query.message.reply_text(
        f"❌ <b>Отменено в {now_msk} МСК</b>\n"
        f"Slug: <code>{slug}</code>\n"
        f"Запись помечена как <code>skipped</code>, draft удалён.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Edit dialog — global handlers + bot_data state (PREV-03, T-2-04)
#
# Edit-сессия живёт в app.bot_data['pending_edits'][chat_id]:
#     {slug, draft_path, expected_sha8, started_at,
#      preview_message_id, preview_chat_id, original_kb}
# Manual timeout check (now - started_at > EDIT_TIMEOUT_S) внутри handle_edit_text;
# ConversationHandler не используется из-за PTB limit: check_update требует
# update.effective_user, у channel_post он всегда None → handler не маршрутизируется.
# ---------------------------------------------------------------------------

_EDIT_INVITE_TEXT: Final[str] = (
    "✏️ <b>Что поправить?</b>\n"
    "\n"
    "Ответь на это сообщение текстом — Claude перепишет пост и пришлёт "
    "новое preview с теми же кнопками.\n"
    "\n"
    "<b>Примеры правок:</b>\n"
    "• <code>сделай короче</code>\n"
    "• <code>убери эмодзи</code>\n"
    "• <code>добавь акцент на бесплатность</code>\n"
    "• <code>замени ссылку на diktumweb.ru</code>\n"
    "• <code>убери последнее предложение</code>\n"
    "\n"
    "<i>Лимит 3 правки на пост · таймаут 10 мин · /cancel_edit чтобы вернуть кнопки</i>"
)


def _pending_edits(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Lazy-init shared dict в bot_data."""
    return ctx.application.bot_data.setdefault("pending_edits", {})


def _edit_message(update: Update):
    """Достать сообщение из update — channel_post или message. None если нет."""
    return update.channel_post or update.message or update.effective_message


async def handle_edit_entry(update: Update,
                              ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped ✏️ Правь — strip buttons, prompt edit text, save state в bot_data."""
    query = update.callback_query
    await query.answer()
    if not await _check_owner(query, ctx):
        return

    m = _CALLBACK_DATA_RE.match(query.data or "")
    if not m or m.group(1) != "edit":
        return
    _, slug, sha8 = m.group(1), m.group(2), m.group(3)

    draft_path = _resolve_draft_path(ctx, slug)
    if not await _verify_draft_sha(query, draft_path, sha8):
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        return   # already handled

    preview_chat_id = query.message.chat_id
    _pending_edits(ctx)[preview_chat_id] = {
        "slug": slug,
        "draft_path": str(draft_path),
        "expected_sha8": sha8,
        "started_at": time.time(),
        "preview_message_id": query.message.message_id,
        "preview_chat_id": preview_chat_id,
    }
    sys.stderr.write(
        f"INFO: handle_edit_entry seeded pending_edits chat_id={preview_chat_id} "
        f"slug={slug} sha8={sha8}\n"
    )

    await query.message.reply_text(_EDIT_INVITE_TEXT, parse_mode=ParseMode.HTML)


async def _restore_buttons_for_state(ctx: ContextTypes.DEFAULT_TYPE,
                                       state: dict, reason: str) -> None:
    """Restore inline keyboard на старом preview + notify в чате."""
    try:
        await ctx.bot.edit_message_reply_markup(
            chat_id=state["preview_chat_id"],
            message_id=state["preview_message_id"],
            reply_markup=_build_inline_kb(state["slug"], state["expected_sha8"]),
        )
    except Exception as exc:
        sys.stderr.write(f"WARN: restore buttons failed: {exc!r}\n")
    try:
        await ctx.bot.send_message(
            chat_id=state["preview_chat_id"],
            text=reason,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        sys.stderr.write(f"WARN: restore notify failed: {exc!r}\n")


async def handle_edit_text(update: Update,
                             ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global handler — ловит все non-command messages (regular + channel_post).

    Filter в build_application = filters.UpdateType.MESSAGES & ~filters.COMMAND.
    filters.TEXT в PTB НЕ матчит channel_post, поэтому проверка text-payload
    делается здесь. Если text отсутствует — silent return.
    """
    msg = _edit_message(update)
    msg_kind = (
        "channel_post" if update.channel_post else
        "message" if update.message else
        "effective" if update.effective_message else
        "none"
    )
    sys.stderr.write(
        f"INFO: handle_edit_text fired msg_kind={msg_kind} "
        f"chat_id={msg.chat_id if msg else None} "
        f"has_text={bool(msg and msg.text)}\n"
    )
    if msg is None or not msg.text:
        return

    chat_id = msg.chat_id
    pending = _pending_edits(ctx)
    state = pending.get(chat_id)
    sys.stderr.write(
        f"INFO: handle_edit_text state={'present' if state else 'absent'} "
        f"pending_keys={list(pending.keys())}\n"
    )
    if state is None:
        return   # not in edit mode — silent ignore

    # Manual timeout check (no JobQueue)
    if time.time() - state["started_at"] > EDIT_TIMEOUT_S:
        pending.pop(chat_id, None)
        await _restore_buttons_for_state(
            ctx, state,
            "⏰ <b>Таймаут правок (10 мин)</b>. Кнопки возвращены.",
        )
        return

    # Owner check: на single-operator канале запись приходит без from_user
    # ("as channel"). state существует только для preview_chat_id (заведён
    # после owner-prove callback от ✏️ Правь) → факт наличия state и
    # совпадения chat_id уже доказывает что говорим с owner. Дополнительно
    # принимаем личку от owner если sender_id == owner_user_id.
    owner_user_id = ctx.application.bot_data["owner_chat_id"]
    preview_chat = ctx.application.bot_data.get("preview_chat_id")
    sender_id = msg.from_user.id if msg.from_user else None
    if chat_id != preview_chat and sender_id != owner_user_id:
        return   # foreign chat — leave state intact

    instruction = msg.text
    draft_path = Path(state["draft_path"])
    spend_file = ctx.application.bot_data["spend_file"]
    repo_root = ctx.application.bot_data["repo_root"]

    progress = await msg.reply_text("⏳ Перегенерирую...")

    try:
        await asyncio.to_thread(regen_one, draft_path, instruction, spend_file)
    except BrandViolationError as exc:
        words: set[str] = set()
        for value in exc.violations.values():
            if isinstance(value, dict):
                for vs in value.values():
                    for v in vs:
                        w = getattr(v, "word", None)
                        if w:
                            words.add(str(w))
        try:
            await progress.edit_text(
                f"⚠️ Не могу применить — <b>brand-lint hard-fail</b>:\n"
                f"<code>{', '.join(sorted(words))}</code>\n\n"
                f"Перефразируй правку (или /cancel_edit).",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return   # T-2-04: state stays, юзер re-tries
    except GenerationError as exc:
        msg_str = str(exc)
        if "regen limit" in msg_str:
            try:
                await progress.edit_text(
                    f"⚠️ Достигнут лимит {MAX_REGEN_PER_DRAFT} правок на draft.\n"
                    f"Используй ❌ Отмена и сгенерируй заново.",
                )
            except Exception:
                pass
            pending.pop(chat_id, None)
            return
        try:
            await progress.edit_text(
                f"⚠️ Regen fail: <code>{msg_str[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return   # state stays, юзер re-tries
    except BudgetExceededError as exc:
        try:
            await progress.edit_text(
                f"⚠️ Бюджет API на месяц исчерпан: <code>{str(exc)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        pending.pop(chat_id, None)
        return

    # Success — send new preview with updated sha8, clear state
    new_sha8 = _draft_sha8(draft_path)
    try:
        await progress.delete()
    except Exception:
        pass

    await _send_preview_for_draft(
        ctx.bot,
        state["preview_chat_id"],
        draft_path,
        new_sha8,
        repo_root,
    )
    pending.pop(chat_id, None)


async def handle_edit_cancel(update: Update,
                               ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel_edit — restore buttons на старом preview + exit edit-mode."""
    msg = _edit_message(update)
    if msg is None:
        return
    chat_id = msg.chat_id
    pending = _pending_edits(ctx)
    state = pending.pop(chat_id, None)
    if state is None:
        return   # not in edit mode — silent
    await _restore_buttons_for_state(
        ctx, state,
        "↩️ <b>Правки отменены</b>. Кнопки возвращены.",
    )


# ---------------------------------------------------------------------------
# Pre-flight + lifecycle
# ---------------------------------------------------------------------------

async def _handle_expired(entry: PlanEntry, draft_path: Path, ctx_dict: dict) -> None:
    """D-2-04 — strip stale kb, mark status: expired, delete draft, tg_nudge alert."""
    bot = ctx_dict["bot"]
    # Stale kb sits in preview_chat_id (где slать preview), не owner_chat_id.
    preview_chat_id = ctx_dict.get("preview_chat_id") or ctx_dict["owner_chat_id"]
    plan_path = ctx_dict["plan_path"]
    repo_root = ctx_dict["repo_root"]

    if draft_path.exists():
        try:
            draft = frontmatter.load(draft_path)
            stale_msg_id = draft.metadata.get("preview_message_id")
            if stale_msg_id and bot is not None:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=preview_chat_id,
                        message_id=int(stale_msg_id),
                        reply_markup=None,
                    )
                except BadRequest:
                    pass   # message already edited / deleted
                except Exception as exc:
                    sys.stderr.write(f"WARN: stale kb strip failed: {exc!r}\n")
        except Exception as exc:
            sys.stderr.write(f"WARN: stale draft load failed: {exc!r}\n")

    # Mutate plan entry status → expired
    try:
        await asyncio.to_thread(
            set_entry_status,
            plan_path, repo_root, entry.slug, "expired",
            {"expired_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        )
    except Exception as exc:
        sys.stderr.write(f"WARN: set_entry_status(expired) failed: {exc!r}\n")

    # Delete local draft
    try:
        draft_path.unlink(missing_ok=True)
    except OSError:
        pass

    # tg_nudge alert
    try:
        await asyncio.to_thread(
            tg_nudge.send,
            "draft_expired",
            slug=entry.slug,
            date=entry.date.isoformat(),
            plan_path=str(plan_path.relative_to(repo_root))
            if plan_path else "(unknown)",
        )
    except KeyError:
        # Template "draft_expired" may not exist — fallback to plain alert
        sys.stderr.write(
            f"INFO: draft_expired template missing; entry={entry.slug} expired\n"
        )
    except Exception as exc:
        sys.stderr.write(f"WARN: tg_nudge draft_expired failed: {exc!r}\n")


async def _alert_generation_failure(entry: PlanEntry, exc: Exception,
                                       ctx_dict: dict) -> None:
    """T-2-05: Claude outage / brand violation / budget cap → tg_nudge alert."""
    reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    sys.stderr.write(f"ERROR: generation failure for {entry.slug}: {reason}\n")
    try:
        await asyncio.to_thread(
            tg_nudge.send,
            "daily_generation_failure",
            slug=entry.slug,
            date=entry.date.isoformat(),
            reason=reason,
        )
    except KeyError:
        sys.stderr.write(
            f"INFO: daily_generation_failure template missing; "
            f"entry={entry.slug} skipped\n"
        )
    except Exception as e:
        sys.stderr.write(f"WARN: tg_nudge alert failed: {e!r}\n")


async def pre_flight_generate(app: Application | None, ctx_dict: dict) -> dict:
    """For each today_entry — classify and act.

    Returns:
        {"should_poll": bool, "pending_slugs": list[str]}
        If should_poll=False → main exits without entering run_polling.
    """
    plan_path = ctx_dict.get("plan_path")
    if plan_path is None or not plan_path.exists():
        sys.stderr.write("INFO: no current month plan — skip\n")
        return {"should_poll": False, "pending_slugs": []}

    try:
        plan = parse_plan(plan_path)
    except Exception as exc:
        sys.stderr.write(f"ERROR: parse_plan failed: {exc!r}\n")
        return {"should_poll": False, "pending_slugs": []}

    today = _today_msk()
    entries = get_today_entries(plan, today)
    if not entries:
        sys.stderr.write(f"INFO: no entries for {today.isoformat()}\n")
        return {"should_poll": False, "pending_slugs": []}

    drafts_dir = ctx_dict["drafts_dir"]
    repo_root = ctx_dict["repo_root"]
    spend_file = ctx_dict["spend_file"]
    bot = ctx_dict["bot"]
    # preview_chat_id = channel «Планировщик» (send target);
    # backward-compat: если ctx_dict не содержит preview_chat_id (старые тесты)
    # — fallback на owner_chat_id (single-operator scenario где они совпадают).
    preview_chat_id = ctx_dict.get("preview_chat_id") or ctx_dict["owner_chat_id"]
    pending: list[str] = []

    for entry in entries:
        state = _classify_entry_state(entry, drafts_dir)
        draft_path = drafts_dir / f"{entry.slug}.md"

        if state == "fresh":
            try:
                generated_path = await asyncio.to_thread(
                    generate_one, entry, repo_root, spend_file, drafts_dir,
                )
                sha8 = _draft_sha8(generated_path)
                msg_id = await _send_preview_for_draft(
                    bot, preview_chat_id, generated_path, sha8, repo_root,
                )
                _store_message_id(generated_path, msg_id)
                pending.append(entry.slug)
            except (BudgetExceededError, BrandViolationError,
                     GenerationError) as exc:
                await _alert_generation_failure(entry, exc, ctx_dict)
                # Mark skipped — не retry forever
                try:
                    await asyncio.to_thread(
                        set_entry_status,
                        plan_path, repo_root, entry.slug, "skipped",
                        {"skipped_reason": str(exc)[:200],
                         "skipped_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                    )
                except Exception as e:
                    sys.stderr.write(f"WARN: skipped mutation failed: {e!r}\n")

        elif state == "expired":
            await _handle_expired(entry, draft_path, ctx_dict)

        elif state == "pending":
            pending.append(entry.slug)
        # approved / skipped → no-op

    return {
        "should_poll": bool(pending),
        "pending_slugs": pending,
    }


def build_application(token: str) -> Application:
    """Build PTB Application с глобальными хендлерами (no ConversationHandler).

    Порядок: (1) edit-CallbackQuery FIRST, чтобы edit: callback не съел catch-all;
    (2) /cancel_edit как MessageHandler+Regex (CommandHandler по умолчанию не
    срабатывает в каналах); (3) handle_edit_text по channel_post & TEXT;
    (4) catch-all CallbackQuery для publish:/cancel:.

    State edit-сессии живёт в app.bot_data['pending_edits'] dict (init на пустой).
    """
    defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    app = (
        Application.builder()
        .token(token)
        .defaults(defaults)
        .concurrent_updates(False)   # single-operator — no need for parallel updates
        .build()
    )
    app.bot_data["pending_edits"] = {}

    # 1. ✏️ Правь — entry в edit-сессию
    app.add_handler(CallbackQueryHandler(handle_edit_entry, pattern=r"^edit:"))

    # КРИТИЧНО: в PTB filters.UpdateType.MESSAGES ИСКЛЮЧИТЕЛЬНО для
    # update.message + update.edited_message (НЕ channel_post). Для каналов
    # есть отдельный filters.UpdateType.CHANNEL_POSTS (channel_post +
    # edited_channel_post). Без OR обоих фильтров правка в канале
    # «Планировщик» не доходит до handler. Это была регрессия PR #40/#46.
    _msg_or_channel = (
        filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS
    )

    # 2. /cancel_edit — работает и в личке, и в канале
    app.add_handler(MessageHandler(
        _msg_or_channel & filters.Regex(r"^/cancel_edit(\s|$|@)"),
        handle_edit_cancel,
    ))

    # 3. Текст правки — глобальный handler, ловит channel_post или message.
    # filters.TEXT тоже channel-post-blind, поэтому фильтра по TEXT нет —
    # проверка msg.text делается внутри handle_edit_text.
    app.add_handler(MessageHandler(
        _msg_or_channel & ~filters.COMMAND,
        handle_edit_text,
    ))

    # 4. Catch-all для publish:/cancel: (registered last)
    app.add_handler(CallbackQueryHandler(handle_publish_or_cancel))
    return app


def _env(key: str) -> str:
    """Read env, fail loudly if missing — but don't print VALUE in error."""
    try:
        val = os.environ[key]
    except KeyError:
        sys.stderr.write(f"FATAL: required env var {key!r} not set\n")
        sys.exit(1)
    if not val:
        sys.stderr.write(f"FATAL: env var {key!r} is empty\n")
        sys.exit(1)
    return val


def main() -> None:
    token = _env("TG_PLANNER_BOT_TOKEN")
    # owner_chat_id = personal user_id (positive) — used by _check_owner against
    # query.from_user.id. NOT used as send target (bot can't initiate DM to user
    # who hasn't /start'ed it; Phase 1.5 lesson re-encountered).
    owner_user_id = int(_env("TG_OWNER_USER_ID"))
    # preview_chat_id = channel id «Forton Lab Планировщик» (negative for channels,
    # positive if it's a personal chat with bot already started). Used as send
    # target for previews + alerts. Phase 1.5 tg_nudge.send посылает туда же.
    preview_chat_id = int(_env("TG_OWNER_CHAT_ID"))
    # ANTHROPIC_API_KEY + BOT_DISPATCH_PAT validated by daily_post_generator / plan_writer на use
    _env("ANTHROPIC_API_KEY")
    _env("BOT_DISPATCH_PAT")

    repo_root = Path.cwd()
    plans_dir = repo_root / PLANS_DIR_NAME
    drafts_dir = repo_root / DRAFTS_DIR_NAME
    spend_file = repo_root / SPEND_FILE_REL
    today = _today_msk()
    plan_path = plans_dir / f"monthly_plan_{today.strftime('%Y-%m')}.md"

    app = build_application(token)
    app.bot_data["owner_chat_id"] = owner_user_id   # for _check_owner
    app.bot_data["preview_chat_id"] = preview_chat_id   # for send targets
    app.bot_data["plans_dir"] = plans_dir
    app.bot_data["drafts_dir"] = drafts_dir
    app.bot_data["repo_root"] = repo_root
    app.bot_data["spend_file"] = spend_file
    app.bot_data["plan_path"] = plan_path

    ctx_dict = {
        "plan_path": plan_path,
        "drafts_dir": drafts_dir,
        "repo_root": repo_root,
        "spend_file": spend_file,
        "bot": app.bot,
        "owner_chat_id": owner_user_id,
        "preview_chat_id": preview_chat_id,
    }

    # Pre-flight + run_polling должны делить ОДИН event loop, иначе
    # PTB run_polling в Python 3.12 падает с RuntimeError "no current event loop"
    # после asyncio.run() закрыл первый loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(pre_flight_generate(app, ctx_dict))
    except Exception:
        loop.close()
        raise

    if not result["should_poll"]:
        loop.close()
        sys.stderr.write("INFO: nothing to poll for — exit 0\n")
        return

    sys.stderr.write(
        f"INFO: pending_slugs={result['pending_slugs']}; "
        f"entering polling for {POLL_TIMEOUT_S}s\n"
    )
    # PTB run_polling сам управляет loop: использует текущий (который мы set_event_loop).
    # drop_pending_updates=True: callbacks queued между cron-окнами (бот спит
    # 22:00 UTC - 09:00 UTC) НЕ обрабатываются на следующий тик. Incident
    # 2026-05-11: юзер нажал Отмена ночью когда бот не запущен, callback
    # сидел в TG-очереди до 12:00 МСК — следующий cron обработал бы и убил
    # запись со skipped. drop_pending_updates=True исключает stale callbacks
    # из out-of-window периода. Активные callbacks внутри текущего polling-
    # окна (между .run_polling и stop_running) обрабатываются как раньше.
    app.run_polling(
        timeout=POLL_TIMEOUT_S,
        allowed_updates=["callback_query", "message", "channel_post"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":   # pragma: no cover
    main()
