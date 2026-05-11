"""Snapshot persistence — храним прошлую неделю на диске для Δ WoW.

Файл: .metrics/store_snapshots.json
Format (per ISO week):
    {
      "2026-W19": {
        "centry": {"app_store": {...StoreSnapshot...}, "google_play": {...}, ...},
        "diktum": {...}
      },
      "2026-W20": {...}
    }

Храним последние 8 недель, старые автоматически чистим (для 4-week trend +
1 неделя prev hard requirement).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

from .models import StoreSnapshot, TrendPoint

MAX_WEEKS_KEPT = 8


def _week_key(date: dt.date) -> str:
    """ISO week key 'YYYY-Www' for stable JSON sorting."""
    iso = date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _iso_week_start(date: dt.date) -> dt.date:
    """Понедельник недели, в которую попадает date."""
    return date - dt.timedelta(days=date.weekday())


def load(path: Path) -> dict:
    """Загрузить snapshots dict; пустой если файл нет/невалидный."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(path: Path, data: dict) -> None:
    """Записать с pretty-print, prune старых недель."""
    pruned = _prune(data, MAX_WEEKS_KEPT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pruned, indent=2, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _prune(data: dict, keep: int) -> dict:
    """Оставить только N последних weeks (по сортировке ключей)."""
    weeks = sorted(data.keys())
    if len(weeks) <= keep:
        return data
    keep_set = set(weeks[-keep:])
    return {k: v for k, v in data.items() if k in keep_set}


def store_week(data: dict, snapshots: list[StoreSnapshot]) -> dict:
    """Добавить/обновить запись по неделе. Mutates and returns data."""
    if not snapshots:
        return data
    week = _week_key(snapshots[0].week_start)
    week_bucket = data.setdefault(week, {})
    for snap in snapshots:
        prod_bucket = week_bucket.setdefault(snap.product, {})
        prod_bucket[snap.store] = asdict(snap)
        # week_start → ISO string for JSON serialisation
        prod_bucket[snap.store]["week_start"] = snap.week_start.isoformat()
    return data


def _snap_from_dict(d: dict) -> StoreSnapshot:
    return StoreSnapshot(
        product=d["product"],
        store=d["store"],
        week_start=dt.date.fromisoformat(d["week_start"]),
        installs=d.get("installs"),
        uninstalls=d.get("uninstalls"),
        rating=d.get("rating"),
        rating_count=d.get("rating_count"),
        top_country=d.get("top_country"),
        top_country_share=d.get("top_country_share"),
        error=d.get("error"),
    )


def get_prev_week(data: dict, current_week_start: dt.date,
                   product: str) -> list[StoreSnapshot]:
    """Snapshots за неделю ДО current_week_start для одного продукта."""
    prev_week_start = current_week_start - dt.timedelta(days=7)
    prev_key = _week_key(prev_week_start)
    bucket = data.get(prev_key, {}).get(product, {})
    return [_snap_from_dict(v) for v in bucket.values()]


def get_4w_trend(data: dict, current_week_start: dt.date,
                   product: str) -> list[TrendPoint]:
    """Последние 4 недели installs (sum across stores) для тренда."""
    points: list[TrendPoint] = []
    for offset in range(3, -1, -1):
        wk_start = current_week_start - dt.timedelta(days=7 * offset)
        wk_key = _week_key(wk_start)
        bucket = data.get(wk_key, {}).get(product, {})
        installs_sum = 0
        had_any = False
        for v in bucket.values():
            i = v.get("installs")
            if i is not None:
                installs_sum += i
                had_any = True
        points.append(TrendPoint(
            week_start=wk_start,
            installs=installs_sum if had_any else None,
        ))
    return points
