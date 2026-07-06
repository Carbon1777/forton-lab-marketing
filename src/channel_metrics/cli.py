"""CLI entrypoint — собирает подписчиков TG/VK/YouTube, рендерит дайджест,
шлёт в TG-канал «Планировщик», сохраняет снапшот для Δ WoW.

Вызывается из .github/workflows/channel_metrics.yml (cron Пн, offset от
store_metrics). Все фетчеры «never raise» — сбоящий канал даёт error-строку,
дайджест всё равно уходит.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import Final

import requests

from . import instagram, telegram, vk, youtube
from .digest import render_channel_digest
from .models import ChannelReport, ChannelSnapshot
from .snapshot import _iso_week_start, get_prev_week, load, save, store_week

SNAPSHOTS_PATH: Final[Path] = Path(".metrics/channel_snapshots.json")
PUBLISHED_DIR: Final[Path] = Path("published")

# Префикс даты в имени published/<YYYY-MM-DD>-slug.md — источник даты публикации.
_PUB_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")


def count_published_in_week(week_start: dt.date,
                            published_dir: Path | None = None) -> int:
    """Число постов, опубликованных за ISO-неделю [week_start, week_start+6].

    Дата берётся из префикса имени файла — присутствует у всех published/*.md.
    Файлы без валидного префикса пропускаются. Нет папки → 0.
    """
    if published_dir is None:
        published_dir = PUBLISHED_DIR
    if not published_dir.exists():
        return 0
    week_end = week_start + dt.timedelta(days=6)
    count = 0
    for p in published_dir.glob("*.md"):
        m = _PUB_DATE_RE.match(p.name)
        if not m:
            continue
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if week_start <= d <= week_end:
            count += 1
    return count


def collect_all(week_start: dt.date) -> list[ChannelSnapshot]:
    """Каждый канал → fetch_weekly с двойной страховкой от исключений.

    Функции резолвятся по атрибуту модуля на момент вызова (не кэшируются в
    module-level списке) — так patch(...fetch_weekly) в тестах работает.
    """
    adapters = [
        ("telegram", telegram.fetch_weekly),
        ("vk", vk.fetch_weekly),
        ("youtube", youtube.fetch_weekly),
        ("instagram", instagram.fetch_weekly),
    ]
    snaps: list[ChannelSnapshot] = []
    for platform, fetch in adapters:
        try:
            snaps.append(fetch(week_start))
        except Exception as exc:  # noqa: BLE001 — fetchers уже ловят, это ремень+подтяжки
            snaps.append(ChannelSnapshot(
                platform=platform, week_start=week_start, subscribers=None,
                error=f"{type(exc).__name__}: {str(exc)[:80]}",
            ))
    return snaps


def build_report(week_start: dt.date, snapshots_data: dict,
                 current_snaps: list[ChannelSnapshot],
                 posts_published: int | None = None) -> ChannelReport:
    prev = get_prev_week(snapshots_data, week_start)
    return ChannelReport(
        week_start=week_start, snapshots=current_snaps, prev_snapshots=prev,
        posts_published=posts_published,
    )


def send_to_planner(digest: str) -> bool:
    """POST sendMessage в TG-канал «Планировщик» через TG_PLANNER_BOT_TOKEN.

    Returns True on success, False on error (workflow не падает — дайджест
    хотя бы напечатан в лог).
    """
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — digest не отправлен\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": digest,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        sys.stderr.write(f"ERROR: TG sendMessage HTTP {r.status_code}: {r.text[:200]}\n")
        return False
    except requests.RequestException as exc:
        sys.stderr.write(f"ERROR: TG send failed: {exc!r}\n")
        return False


def main(today: dt.date | None = None,
         snapshots_path: Path | None = None,
         published_dir: Path | None = None) -> int:
    """Entry для workflow. today=None → date.today() (UTC на runner).

    Снимок подписчиков ключуется ТЕКУЩЕЙ ISO-неделей (point-in-time), Δ WoW
    считается против снимка прошлой недели. Счётчик постов — за ПРОШЛУЮ
    завершённую неделю (запуск в понедельник ⇒ «сколько опубликовали за неделю»).
    """
    if today is None:
        today = dt.date.today()
    if snapshots_path is None:
        snapshots_path = SNAPSHOTS_PATH

    week_start = _iso_week_start(today)
    sys.stderr.write(
        f"INFO: channel_metrics digest for week {week_start.isoformat()}\n"
    )

    prev_week_start = week_start - dt.timedelta(days=7)
    posts_published = count_published_in_week(prev_week_start, published_dir)

    data = load(snapshots_path)
    current_snaps = collect_all(week_start)
    report = build_report(week_start, data, current_snaps,
                          posts_published=posts_published)

    digest = render_channel_digest(report)
    print(digest)  # для GH Actions log
    ok = send_to_planner(digest)

    data = store_week(data, current_snaps)
    save(snapshots_path, data)

    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
