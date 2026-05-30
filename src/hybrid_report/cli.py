"""Entrypoint — гибридный per-app отчёт.

Loop по PRODUCTS: собирает все источники, рендерит word-based сообщение,
шлёт ОТДЕЛЬНОЕ сообщение на КАЖДЫЙ продукт в TG-канал «Планировщик»,
сохраняет hybrid-снапшот для WoW.

Поддерживает --dry-run (или env HYBRID_DRY_RUN=1): печать в STDOUT без TG,
снапшот НЕ пишется. Вызывается из .github/workflows/store_metrics.yml (Пн).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Final

import requests

from . import gather, render, snapshot
from .models import PRODUCTS

SNAPSHOTS_PATH: Final[Path] = Path(".metrics/hybrid_snapshots.json")


def _iso_week_start(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


def _report_week(today: dt.date) -> tuple[dt.date, dt.date]:
    """Прошлая ISO-неделя: (понедельник, воскресенье)."""
    last_monday = _iso_week_start(today) - dt.timedelta(days=7)
    return last_monday, last_monday + dt.timedelta(days=6)


def send_to_planner(text: str) -> bool:
    """sendMessage в TG-канал «Планировщик» (TG_PLANNER_BOT_TOKEN/CHAT_ID).

    Текст plain (без HTML-разметки) → parse_mode НЕ задаём. Не падает:
    возвращает True/False.
    """
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — сообщение не отправлено\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        sys.stderr.write(f"ERROR: TG HTTP {r.status_code}: {r.text[:200]}\n")
        return False
    except requests.RequestException as exc:
        sys.stderr.write(f"ERROR: TG send failed: {exc!r}\n")
        return False


def _resolve_dry_run(dry_run: bool | None) -> bool:
    if dry_run is not None:
        return dry_run
    if "--dry-run" in sys.argv:
        return True
    return os.environ.get("HYBRID_DRY_RUN") == "1"


def main(
    today: dt.date | None = None,
    snapshots_path: Path | None = None,
    dry_run: bool | None = None,
) -> int:
    today = today or dt.date.today()
    snapshots_path = snapshots_path or SNAPSHOTS_PATH
    dry = _resolve_dry_run(dry_run)

    week_start, week_end = _report_week(today)
    sys.stderr.write(
        f"INFO: hybrid report for {week_start}–{week_end} "
        f"(dry_run={dry})\n"
    )

    data = snapshot.load(snapshots_path)
    ok = True
    for spec in PRODUCTS:
        report = gather.gather_product(spec, week_start, week_end, data)
        text = render.render_report(report)
        print(text)  # для GH Actions log / STDOUT (dry-run)
        print()      # разделитель между продуктами в логе
        if not dry:
            ok = send_to_planner(text) and ok
        # снапшот текущей недели для следующего WoW (даже если TG не ушёл)
        snapshot.store_week(data, week_start, spec.key, report.am_installs_total)

    if not dry:
        snapshot.save(snapshots_path, data)

    return 0 if (ok or dry) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
