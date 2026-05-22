"""Snapshot persistence — храним прошлые недели для Δ WoW.

Файл: .metrics/centry_funnel_snapshots.json
Format (per ISO week):
    {"2026-W20": {"week_start": "2026-05-11", "installs_total": 18,
                  "installs_organic": 16, "installs_ads": 2,
                  "new_profiles": 7, "guests": 5, "users": 2, "activations": 1}}
Храним последние 8 недель.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .models import FunnelWeek

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


def store_week(data: dict, fw: FunnelWeek) -> dict:
    """Добавить/обновить запись недели. Mutates and returns data."""
    data[_week_key(fw.week_start)] = {
        "week_start": fw.week_start.isoformat(),
        "installs_total": fw.installs_total,
        "installs_organic": fw.installs_organic,
        "installs_ads": fw.installs_ads,
        "new_profiles": fw.new_profiles,
        "guests": fw.guests,
        "users": fw.users,
        "activations": fw.activations,
    }
    return data


def get_prev(data: dict, current_week_start: dt.date) -> dict | None:
    """Запись за неделю ДО current_week_start, или None."""
    prev_week_start = current_week_start - dt.timedelta(days=7)
    return data.get(_week_key(prev_week_start))
