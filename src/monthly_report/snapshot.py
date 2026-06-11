"""Monthly snapshot — per-month per-product AppMetrica installs для MoM.

File: .metrics/monthly_snapshots.json
Format:
    {"2026-05": {"centry": {"am_installs_total": 450},
                 "diktum": {"am_installs_total": 310}, ...},
     "2026-06": {...}}
Храним последние 13 месяцев.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_MONTHS_KEPT = 13


def _month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _prune(data: dict, keep: int) -> dict:
    keys = sorted(data.keys())
    if len(keys) <= keep:
        return data
    keep_set = set(keys[-keep:])
    return {k: v for k, v in data.items() if k in keep_set}


def save(path: Path, data: dict) -> None:
    pruned = _prune(data, MAX_MONTHS_KEPT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pruned, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def store_month(
    data: dict,
    year: int,
    month: int,
    product_key: str,
    am_installs_total: int | None,
) -> dict:
    """Добавить/обновить запись месяца для продукта. Mutates and returns data."""
    key = _month_key(year, month)
    data.setdefault(key, {})[product_key] = {"am_installs_total": am_installs_total}
    return data


def get_prev_installs(
    data: dict, year: int, month: int, product_key: str
) -> int | None:
    """Установки за месяц ДО (year, month) для продукта, или None."""
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    rec = data.get(_month_key(prev_year, prev_month), {}).get(product_key)
    if not rec:
        return None
    return rec.get("am_installs_total")
