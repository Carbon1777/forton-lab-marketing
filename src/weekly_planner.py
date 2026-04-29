"""Forton Lab — weekly content planner reminder.

Runs on a schedule (Mon + Thu, 06:00 UTC = 09:00 MSK) via GitHub Actions.
Builds a short digest of last-week activity and posts it to the user's
personal Telegram chat through a dedicated planner bot.

Does NOT publish anything to the public channels. Its only job is to
nudge the user "time to plan posts" — actual publication remains manual.

Environment:
    TG_PLANNER_BOT_TOKEN  — token of @fortonlab_planner_bot
    TG_OWNER_CHAT_ID      — user's personal chat_id (numeric)

Repo layout it inspects:
    published/   — files dated for the last 7 days are counted as "this week"
    queue/       — files awaiting push (excluding _template.md)
    inputs/raw/  — optional, raw user assets pending use (if folder exists)
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = REPO_ROOT / "published"
QUEUE_DIR = REPO_ROOT / "queue"
INPUTS_RAW_DIR = REPO_ROOT / "inputs" / "raw"

TG_API_BASE = "https://api.telegram.org"

# Filenames in published/ start with YYYY-MM-DD (the date tg_post.py used)
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: env {name} is missing\n")
        sys.exit(1)
    return val


def parse_date_from_filename(p: Path) -> dt.date | None:
    m = DATE_RE.match(p.name)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def count_recent_published(days: int = 7) -> tuple[int, list[str]]:
    """Return (count, sample_titles) for files in published/ from last N days."""
    if not PUBLISHED_DIR.exists():
        return 0, []
    cutoff = dt.date.today() - dt.timedelta(days=days)
    recent: list[str] = []
    for p in sorted(PUBLISHED_DIR.glob("*.md")):
        d = parse_date_from_filename(p)
        if d and d >= cutoff:
            # Strip leading date prefix(es) from filename for a readable label.
            stem = re.sub(r"^(\d{4}-\d{2}-\d{2}-)+", "", p.stem)
            recent.append(stem)
    return len(recent), recent


def list_queue() -> list[str]:
    if not QUEUE_DIR.exists():
        return []
    return sorted(
        p.stem for p in QUEUE_DIR.glob("*.md")
        if not p.name.startswith("_")
    )


def list_inputs_raw() -> list[str]:
    if not INPUTS_RAW_DIR.exists():
        return []
    return sorted(p.name for p in INPUTS_RAW_DIR.iterdir() if p.is_file())


def build_digest() -> str:
    today = dt.date.today()
    weekday_ru = ["понедельник", "вторник", "среда", "четверг",
                  "пятница", "суббота", "воскресенье"][today.weekday()]

    pub_count, pub_list = count_recent_published(days=7)
    queue_list = list_queue()
    raw_list = list_inputs_raw()

    lines: list[str] = []
    lines.append(f"📅 <b>{weekday_ru.capitalize()}, {today.isoformat()} — время согласовать посты.</b>")
    lines.append("")

    if pub_count == 0:
        lines.append("За последние 7 дней постов <b>не было</b>.")
    else:
        lines.append(f"За последние 7 дней опубликовано: <b>{pub_count}</b>")
        for name in pub_list[:5]:
            lines.append(f"  · {name}")
        if pub_count > 5:
            lines.append(f"  · …ещё {pub_count - 5}")
    lines.append("")

    if queue_list:
        lines.append(f"В <code>queue/</code> ждёт публикации: {len(queue_list)}")
        for name in queue_list[:5]:
            lines.append(f"  · {name}")
    else:
        lines.append("В <code>queue/</code> пусто.")
    lines.append("")

    if raw_list:
        lines.append(f"В <code>inputs/raw/</code> сырьё: {len(raw_list)} файл(а/ов)")
        for name in raw_list[:5]:
            lines.append(f"  · {name}")
        if len(raw_list) > 5:
            lines.append(f"  · …ещё {len(raw_list) - 5}")
    else:
        lines.append("В <code>inputs/raw/</code> сырья нет.")
    lines.append("")

    lines.append("Открой Cowork — обсудим план на ближайшие дни.")

    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> dict:
    url = f"{TG_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> int:
    token = env("TG_PLANNER_BOT_TOKEN")
    chat_id = env("TG_OWNER_CHAT_ID")

    text = build_digest()
    print("=== digest ===")
    print(text)
    print("=== sending ===")

    result = send_telegram(token, chat_id, text)
    if not result.get("ok"):
        sys.stderr.write(f"FAIL: {result}\n")
        return 1
    print(f"✓ sent (message_id={result['result']['message_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
