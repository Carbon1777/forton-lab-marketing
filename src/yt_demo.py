"""yt_demo — one-shot Google verification demo flow.

Standalone PTB script. Sends preview видео в TG-канал «Планировщик» с
inline-кнопкой ✅ "Опубликуй на YouTube", на approve — uploads video на
YouTube (privacy=unlisted) через те же creds что и youtube_post.py.

НЕ читает plan/drafts. НЕ пишет в published/. Не делает git commit.
Только TG preview + YouTube upload. Channel filter не используется.

Env required:
    TG_PLANNER_BOT_TOKEN, TG_OWNER_USER_ID, TG_OWNER_CHAT_ID,
    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN,
    DEMO_VIDEO_PATH (path to mp4, может быть assets/demo/...mp4),
    DEMO_TITLE (default "Forton Lab — OAuth verification demo"),
    DEMO_DESCRIPTION (default — стандартный текст про verification).

Usage (workflow_dispatch only):
    PYTHONPATH=. python -m src.yt_demo
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    Defaults,
)

from src.youtube_post import build_credentials, upload_video

POLL_TIMEOUT_S = 540   # 9 min — matches preview_bot


def _env(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if not val:
        sys.stderr.write(f"FATAL: env {key!r} missing\n")
        sys.exit(1)
    return val


async def _check_owner(query, owner_id: int) -> bool:
    if query.from_user.id != owner_id:
        try:
            await query.answer("Not authorized", show_alert=False)
        except Exception:
            pass
        sys.stderr.write(
            f"WARN: callback from non-owner user_id={query.from_user.id}\n"
        )
        return False
    return True


async def handle_callback(update: Update,
                           ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    owner_id = ctx.application.bot_data["owner_user_id"]
    sys.stderr.write(
        f"INFO: yt_demo callback fired data={query.data!r} "
        f"from_user={query.from_user.id}\n"
    )
    try:
        await query.answer()
    except Exception as exc:
        sys.stderr.write(f"WARN: query.answer failed: {exc!r}\n")
    if not await _check_owner(query, owner_id):
        return

    action = query.data or ""
    if action == "yt_demo_cancel":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "❌ <b>Demo отменён</b>", parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        ctx.application.stop_running()
        return

    if action != "yt_demo_publish":
        sys.stderr.write(f"WARN: unknown callback: {action!r}\n")
        return

    # Strip buttons immediately (idempotency)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:
        sys.stderr.write(f"WARN: strip kb failed: {exc!r}\n")

    video_path = Path(ctx.application.bot_data["video_path"])
    title = ctx.application.bot_data["title"]
    description = ctx.application.bot_data["description"]

    await query.message.reply_text(
        f"⏳ <b>Загружаю на YouTube как unlisted…</b>\n"
        f"<code>{video_path.name}</code>",
        parse_mode=ParseMode.HTML,
    )

    try:
        creds = await asyncio.to_thread(build_credentials)
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        video_id = await asyncio.to_thread(
            upload_video,
            youtube, video_path, title, description,
            ["demo", "forton lab"], "unlisted", "22",   # category=People&Blogs
        )
    except Exception as exc:
        sys.stderr.write(f"ERROR: upload failed: {exc!r}\n")
        try:
            await query.message.reply_text(
                f"❌ <b>Upload failed</b>\n<code>{type(exc).__name__}: "
                f"{str(exc)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        ctx.application.stop_running()
        return

    url = f"https://www.youtube.com/watch?v={video_id}"
    await query.message.reply_text(
        f"✅ <b>Demo загружено</b>\n"
        f"Privacy: <b>unlisted</b>\n"
        f"URL: {url}\n\n"
        f"<i>Скопируй этот URL в форму Google verification как demo video. "
        f"После завершения review можешь удалить видео из YouTube Studio.</i>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )
    ctx.application.stop_running()


async def send_preview(app: Application, chat_id: int,
                        video_path: Path, title: str) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать на YouTube",
                              callback_data="yt_demo_publish"),
        InlineKeyboardButton("❌ Отмена", callback_data="yt_demo_cancel"),
    ]])
    caption = (
        f"🎬 <b>YT-DEMO для Google verification</b>\n\n"
        f"<i>{title}</i>\n\n"
        f"Файл: <code>{video_path.name}</code>\n"
        f"Размер: {video_path.stat().st_size // 1024} КБ\n\n"
        f"<b>На approve видео улетит ТОЛЬКО на YouTube (unlisted).</b>\n"
        f"TG/VK/Дзен <b>не</b> задействованы.\n"
        f"План постов <b>не</b> затронут."
    )
    with video_path.open("rb") as f:
        await app.bot.send_video(
            chat_id=chat_id,
            video=f,
            caption=caption,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
            reply_markup=kb,
        )


def main() -> int:
    token = _env("TG_PLANNER_BOT_TOKEN")
    owner_user_id = int(_env("TG_OWNER_USER_ID"))
    preview_chat_id = int(_env("TG_OWNER_CHAT_ID"))
    _env("YT_CLIENT_ID")
    _env("YT_CLIENT_SECRET")
    _env("YT_REFRESH_TOKEN")
    video_path = Path(_env("DEMO_VIDEO_PATH"))
    title = _env("DEMO_TITLE",
                  "Forton Lab — OAuth verification demo (youtube.upload scope)")
    description = _env("DEMO_DESCRIPTION", (
        "This is an internal demo recording for Google OAuth app verification. "
        "Forton Lab Publisher uses the youtube.upload scope solely to publish "
        "short videos from our internal pipeline to our own YouTube channel "
        "@fortonlab. No third-party users involved. "
        "Privacy policy: https://fortonlab.ru/privacy "
        "Terms: https://fortonlab.ru/terms"
    ))

    if not video_path.exists():
        sys.stderr.write(f"FATAL: video {video_path} does not exist\n")
        return 1

    defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    app = (
        Application.builder()
        .token(token)
        .defaults(defaults)
        .concurrent_updates(False)
        .build()
    )
    app.bot_data["owner_user_id"] = owner_user_id
    app.bot_data["video_path"] = str(video_path)
    app.bot_data["title"] = title
    app.bot_data["description"] = description
    app.add_handler(CallbackQueryHandler(handle_callback))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_preview(app, preview_chat_id, video_path, title))

    sys.stderr.write(
        f"INFO: yt_demo preview sent, entering polling {POLL_TIMEOUT_S}s\n"
    )
    app.run_polling(
        timeout=POLL_TIMEOUT_S,
        allowed_updates=["callback_query"],
        drop_pending_updates=True,
    )
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
