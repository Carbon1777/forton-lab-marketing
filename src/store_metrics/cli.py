"""CLI entrypoint — собирает все 3 стора × 2 продукта, рендерит digest,
шлёт в TG-канал «Планировщик», сохраняет снапшот для следующей недели.

Вызывается из .github/workflows/store_metrics.yml каждый Пн 06:37 UTC.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Final

import requests

from . import asc, play, rustore
from .digest import render_digest
from .models import Product, ProductReport, StoreSnapshot, WeeklyReport
from .snapshot import (
    _iso_week_start,
    get_4w_trend,
    get_prev_week,
    load,
    save,
    store_week,
)

PRODUCTS: Final[list[Product]] = ["centry", "diktum"]
SNAPSHOTS_PATH: Final[Path] = Path(".metrics/store_snapshots.json")


def collect_all(week_start: dt.date) -> list[StoreSnapshot]:
    """For each (product, store) → fetch_weekly with error catching."""
    snaps: list[StoreSnapshot] = []
    adapters = [
        ("app_store", asc.fetch_weekly),
        ("google_play", play.fetch_weekly),
        ("rustore", rustore.fetch_weekly),
    ]
    for product in PRODUCTS:
        for store_name, fetch in adapters:
            try:
                snaps.append(fetch(product, week_start))
            except NotImplementedError as exc:
                snaps.append(StoreSnapshot(
                    product=product, store=store_name,  # type: ignore[arg-type]
                    week_start=week_start, installs=None,
                    error=f"not implemented: {exc}",
                ))
            except Exception as exc:
                snaps.append(StoreSnapshot(
                    product=product, store=store_name,  # type: ignore[arg-type]
                    week_start=week_start, installs=None,
                    error=f"{type(exc).__name__}: {str(exc)[:80]}",
                ))
    return snaps


def build_report(week_start: dt.date, snapshots_data: dict,
                   current_snaps: list[StoreSnapshot]) -> WeeklyReport:
    """Compose WeeklyReport: split current per product + load prev + trend."""
    products: list[ProductReport] = []
    for product in PRODUCTS:
        curr = [s for s in current_snaps if s.product == product]
        prev = get_prev_week(snapshots_data, week_start, product)
        # 4-week trend builds AFTER current week is stored — но для отчёта
        # хотим увидеть W-3 → W-2 → W-1 → W (текущая). Текущая ещё не в data,
        # поэтому добавляем её "виртуально".
        trend = get_4w_trend(snapshots_data, week_start, product)
        # Patch last point with current snapshots sum
        curr_installs = sum((s.installs or 0) for s in curr) if any(s.installs is not None for s in curr) else None
        if trend and curr_installs is not None:
            trend[-1] = trend[-1].__class__(week_start=week_start, installs=curr_installs)
        products.append(ProductReport(
            product=product, snapshots=curr,
            prev_snapshots=prev, trend_4w=trend,
        ))
    return WeeklyReport(week_start=week_start, products=products)


def send_to_planner(digest: str) -> bool:
    """POST sendMessage в TG-канал «Планировщик» через TG_PLANNER_BOT_TOKEN.

    Returns True on success, False on error (workflow не падает — alert ушёл
    хотя бы в stderr).
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
    """Entry для workflow. today=None → datetime.date.today() (UTC на runner)."""
    if today is None:
        today = dt.date.today()
    if snapshots_path is None:
        snapshots_path = SNAPSHOTS_PATH

    # Неделя ОТЧЁТА = неделя, ЗАКОНЧИВШАЯСЯ последним воскресеньем.
    # Т.е. если сегодня Пн 12 мая → отчёт за 5-11 мая (week_start=5 мая).
    last_week_monday = _iso_week_start(today) - dt.timedelta(days=7)
    sys.stderr.write(
        f"INFO: store_metrics digest for week {last_week_monday.isoformat()}\n"
    )

    data = load(snapshots_path)
    current_snaps = collect_all(last_week_monday)
    report = build_report(last_week_monday, data, current_snaps)
    digest = render_digest(report)

    print(digest)   # для GH Actions log
    ok = send_to_planner(digest)

    # Сохраняем снапшот текущей недели для следующего запуска
    data = store_week(data, current_snaps)
    save(snapshots_path, data)

    return 0 if ok else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
