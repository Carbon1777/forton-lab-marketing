"""CLI entrypoint — собирает подписчиков TG/VK/YouTube, рендерит дайджест,
шлёт в TG-канал «Планировщик», сохраняет снапшот для Δ WoW.

Вызывается из .github/workflows/channel_metrics.yml (cron Пн, offset от
store_metrics). Все фетчеры «never raise» — сбоящий канал даёт error-строку,
дайджест всё равно уходит.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Final

import requests

from . import telegram, vk, youtube
from .digest import render_channel_digest
from .models import ChannelReport, ChannelSnapshot
from .snapshot import _iso_week_start, get_prev_week, load, save, store_week

SNAPSHOTS_PATH: Final[Path] = Path(".metrics/channel_snapshots.json")


def collect_all(week_start: dt.date) -> list[ChannelSnapshot]:
    """Каждый канал → fetch_weekly с двойной страховкой от исключений.

    Функции резолвятся по атрибуту модуля на момент вызова (не кэшируются в
    module-level списке) — так patch(...fetch_weekly) в тестах работает.
    """
    adapters = [
        ("telegram", telegram.fetch_weekly),
        ("vk", vk.fetch_weekly),
        ("youtube", youtube.fetch_weekly),
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
                 current_snaps: list[ChannelSnapshot]) -> ChannelReport:
    prev = get_prev_week(snapshots_data, week_start)
    return ChannelReport(
        week_start=week_start, snapshots=current_snaps, prev_snapshots=prev,
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
         snapshots_path: Path | None = None) -> int:
    """Entry для workflow. today=None → date.today() (UTC на runner).

    Снимок ключуется ТЕКУЩЕЙ ISO-неделей (подписчики — point-in-time), Δ WoW
    считается против снимка прошлой недели.
    """
    if today is None:
        today = dt.date.today()
    if snapshots_path is None:
        snapshots_path = SNAPSHOTS_PATH

    week_start = _iso_week_start(today)
    sys.stderr.write(
        f"INFO: channel_metrics digest for week {week_start.isoformat()}\n"
    )

    data = load(snapshots_path)
    current_snaps = collect_all(week_start)
    report = build_report(week_start, data, current_snaps)

    digest = render_channel_digest(report)
    print(digest)  # для GH Actions log
    ok = send_to_planner(digest)

    data = store_week(data, current_snaps)
    save(snapshots_path, data)

    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
