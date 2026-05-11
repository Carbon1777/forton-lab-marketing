"""Domain models — store-agnostic shapes для digest."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

Store = Literal["app_store", "google_play", "rustore"]
Product = Literal["centry", "diktum"]


@dataclass(frozen=True)
class StoreSnapshot:
    """Метрики одного product × store за одну неделю.

    Если стор не вернул значение (RuStore stats unavailable, например) —
    оставляем None. Digest рендерит «—» для None.
    """
    product: Product
    store: Store
    week_start: dt.date          # ISO week start (Mon)
    installs: int | None
    uninstalls: int | None = None
    rating: float | None = None      # 1.0..5.0
    rating_count: int | None = None
    top_country: str | None = None   # "RU", "KZ", etc.
    top_country_share: float | None = None   # 0.0..1.0
    error: str | None = None         # human-readable если стор недоступен


@dataclass(frozen=True)
class WeekDelta:
    """Сравнение текущей и прошлой недели."""
    current: int | None
    previous: int | None
    delta_pct: float | None = None   # (curr-prev)/prev * 100; None если prev=0/None
    arrow: str = "→"                  # "📈" / "📉" / "→" / "—"

    @classmethod
    def compute(cls, current: int | None, previous: int | None,
                significant_pct: float = 5.0) -> "WeekDelta":
        if current is None or previous is None:
            return cls(current=current, previous=previous, delta_pct=None, arrow="—")
        if previous == 0:
            if current == 0:
                return cls(current=0, previous=0, delta_pct=0.0, arrow="→")
            return cls(current=current, previous=0, delta_pct=None, arrow="📈")
        pct = (current - previous) / previous * 100.0
        if pct > significant_pct:
            arrow = "📈"
        elif pct < -significant_pct:
            arrow = "📉"
        else:
            arrow = "→"
        return cls(current=current, previous=previous, delta_pct=pct, arrow=arrow)


@dataclass(frozen=True)
class TrendPoint:
    """Точка 4-недельного тренда — для sparkline."""
    week_start: dt.date
    installs: int | None


@dataclass(frozen=True)
class ProductReport:
    """Отчёт по одному продукту через все сторы + тренд + алерты.

    Список snapshots — текущая неделя по каждому стору.
    Список prev_snapshots — прошлая неделя для Δ WoW.
    trend_4w — последние 4 недели installs (сумма по сторам).
    """
    product: Product
    snapshots: list[StoreSnapshot]
    prev_snapshots: list[StoreSnapshot]
    trend_4w: list[TrendPoint]
    alerts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WeeklyReport:
    """Полный недельный отчёт — все продукты + общие алерты."""
    week_start: dt.date
    products: list[ProductReport]
    overall_alerts: list[str] = field(default_factory=list)
    generated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


# Significant alert threshold — abs(Δ WoW%) >= ALERT_PCT triggers алерт
ALERT_PCT: float = 20.0
