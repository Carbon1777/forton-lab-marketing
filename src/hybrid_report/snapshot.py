"""Snapshot persistence для WoW — per-week per-product установки AppMetrica.

Файл: .metrics/hybrid_snapshots.json
Format (per ISO week, per product):
    {"2026-W21": {"centry": {"am_installs_total": 24},
                  "diktum": {"am_installs_total": 121}}}
Храним последние 8 недель.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

MAX_WEEKS_KEPT = 8


def _week_key(date: dt.date) -> str:
    iso = date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _prune(data: dict, keep: int) -> dict:
    weeks = sorted(data.keys())
    if len(weeks) <= keep:
        return data
    keep_set = set(weeks[-keep:])
    return {k: v for k, v in data.items() if k in keep_set}


def save(path: Path, data: dict) -> None:
    pruned = _prune(data, MAX_WEEKS_KEPT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pruned, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def store_week(
    data: dict,
    week_start: dt.date,
    product_key: str,
    am_installs_total: int | None,
) -> dict:
    """Добавить/обновить запись недели для продукта. Mutates and returns data."""
    key = _week_key(week_start)
    week = data.setdefault(key, {})
    week[product_key] = {"am_installs_total": am_installs_total}
    return data


def get_prev_installs(
    data: dict, current_week_start: dt.date, product_key: str
) -> int | None:
    """Установки за неделю ДО current_week_start для продукта, или None."""
    prev_week_start = current_week_start - dt.timedelta(days=7)
    week = data.get(_week_key(prev_week_start))
    if not week:
        return None
    rec = week.get(product_key)
    if not rec:
        return None
    return rec.get("am_installs_total")
