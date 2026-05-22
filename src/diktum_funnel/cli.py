"""Entrypoint — собирает воронку за прошлую неделю, рендерит, шлёт в ТГ,
сохраняет снапшот. Вызывается из .github/workflows/funnel_metrics.yml.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Final

import requests

from . import appmetrica, supabase_src
from .digest import render_digest
from .models import FunnelWeek
from .snapshot import get_prev, load, save, store_week

SNAPSHOTS_PATH: Final[Path] = Path(".metrics/funnel_snapshots.json")


def _iso_week_start(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


def _report_week(today: dt.date) -> tuple[dt.date, dt.date]:
    """Прошлая ISO-неделя: (понедельник, воскресенье)."""
    last_monday = _iso_week_start(today) - dt.timedelta(days=7)
    return last_monday, last_monday + dt.timedelta(days=6)


def send_to_planner(text: str) -> bool:
    """sendMessage в ТГ-канал «Планировщик» (TG_PLANNER_BOT_TOKEN/CHAT_ID)."""
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — digest не отправлен\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code == 200:
            return True
        sys.stderr.write(f"ERROR: TG HTTP {r.status_code}: {r.text[:200]}\n")
        return False
    except requests.RequestException as exc:
        sys.stderr.write(f"ERROR: TG send failed: {exc!r}\n")
        return False


def _collect(week_start: dt.date, week_end: dt.date) -> FunnelWeek:
    installs_total = installs_organic = installs_ads = None
    appmetrica_error = None
    try:
        inst = appmetrica.fetch_installs(week_start, week_end)
        installs_total, installs_organic, installs_ads = inst.total, inst.organic, inst.ads
    except Exception as exc:  # graceful — отчёт уходит без установок
        appmetrica_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: AppMetrica failed: {appmetrica_error}\n")

    registrations = activated = None
    supabase_error = None
    try:
        db = supabase_src.fetch_registrations(week_start, week_end)
        registrations, activated = db.registrations, db.activated
    except Exception as exc:
        supabase_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: Supabase failed: {supabase_error}\n")

    return FunnelWeek(
        week_start=week_start, week_end=week_end,
        installs_total=installs_total, installs_organic=installs_organic,
        installs_ads=installs_ads, registrations=registrations, activated=activated,
        appmetrica_error=appmetrica_error, supabase_error=supabase_error,
    )


def main(today: dt.date | None = None, snapshots_path: Path | None = None) -> int:
    today = today or dt.date.today()
    snapshots_path = snapshots_path or SNAPSHOTS_PATH

    week_start, week_end = _report_week(today)
    sys.stderr.write(f"INFO: funnel digest for {week_start}–{week_end}\n")

    fw = _collect(week_start, week_end)

    data = load(snapshots_path)
    prev = get_prev(data, week_start)

    digest = render_digest(fw, prev)
    print(digest)   # для GH Actions log
    ok = send_to_planner(digest)

    data = store_week(data, fw)
    save(snapshots_path, data)

    return 0 if ok else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
