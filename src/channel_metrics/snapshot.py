"""Snapshot persistence — храним прошлую неделю на диске для Δ WoW.

Файл: .metrics/channel_snapshots.json (изолирован от store/hybrid снапшотов).
Format (per ISO week):
    {
      "2026-W27": {
        "telegram": {...ChannelSnapshot...},
        "vk": {...},
        "youtube": {...}
      }
    }
Храним последние 8 недель, старые чистим.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

from .models import ChannelSnapshot

MAX_WEEKS_KEPT = 8


def _week_key(date: dt.date) -> str:
    iso = date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _iso_week_start(date: dt.date) -> dt.date:
    """Понедельник недели, в которую попадает date."""
    return date - dt.timedelta(days=date.weekday())


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(path: Path, data: dict) -> None:
    pruned = _prune(data, MAX_WEEKS_KEPT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pruned, indent=2, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _prune(data: dict, keep: int) -> dict:
    weeks = sorted(data.keys())
    if len(weeks) <= keep:
        return data
    keep_set = set(weeks[-keep:])
    return {k: v for k, v in data.items() if k in keep_set}


def store_week(data: dict, snapshots: list[ChannelSnapshot]) -> dict:
    """Добавить/обновить запись по неделе. Mutates and returns data."""
    if not snapshots:
        return data
    week = _week_key(snapshots[0].week_start)
    week_bucket = data.setdefault(week, {})
    for snap in snapshots:
        d = asdict(snap)
        d["week_start"] = snap.week_start.isoformat()
        week_bucket[snap.platform] = d
    return data


def _snap_from_dict(d: dict) -> ChannelSnapshot:
    return ChannelSnapshot(
        platform=d["platform"],
        week_start=dt.date.fromisoformat(d["week_start"]),
        subscribers=d.get("subscribers"),
        reach=d.get("reach"),
        error=d.get("error"),
    )


def get_prev_week(data: dict, current_week_start: dt.date) -> list[ChannelSnapshot]:
    """Snapshots за неделю ДО current_week_start (все платформы, что были)."""
    prev_week_start = current_week_start - dt.timedelta(days=7)
    prev_key = _week_key(prev_week_start)
    bucket = data.get(prev_key, {})
    return [_snap_from_dict(v) for v in bucket.values()]
