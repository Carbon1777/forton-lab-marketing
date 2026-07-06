"""Domain models — платформо-агностичные формы для дайджеста каналов."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

Platform = Literal["telegram", "vk", "youtube", "instagram"]

# Фиксированный порядок рендера + человекочитаемые метки с emoji.
PLATFORM_ORDER: tuple[Platform, ...] = ("telegram", "vk", "youtube", "instagram")
PLATFORM_LABEL: dict[str, str] = {
    "telegram": "🔵 Telegram",
    "vk": "🟦 VK",
    "youtube": "🔴 YouTube",
    "instagram": "🟣 Instagram",
}


@dataclass(frozen=True)
class ChannelSnapshot:
    """Снимок одного канала за одну неделю.

    ``subscribers`` — число подписчиков на момент снятия (point-in-time).
    ``reach`` — недельный охват (VK stats.get); None если недоступно
    (community-токен без stats-доступа, TG/YouTube без метода охвата).
    ``error`` — человекочитаемая причина, если канал не отдал данные;
    дайджест рендерит такую строку как «— (нет данных)», не роняя отчёт.
    """
    platform: Platform
    week_start: dt.date
    subscribers: int | None
    reach: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ChannelDelta:
    """Сравнение подписчиков текущей и прошлой недели (абсолютное)."""
    current: int | None
    previous: int | None
    delta_abs: int | None = None
    arrow: str = "→"

    @classmethod
    def compute(cls, current: int | None, previous: int | None) -> "ChannelDelta":
        if current is None or previous is None:
            return cls(current=current, previous=previous, delta_abs=None, arrow="—")
        d = current - previous
        if d > 0:
            arrow = "📈"
        elif d < 0:
            arrow = "📉"
        else:
            arrow = "→"
        return cls(current=current, previous=previous, delta_abs=d, arrow=arrow)


@dataclass(frozen=True)
class ChannelReport:
    """Полный недельный отчёт по каналам студии.

    ``posts_published`` — сколько постов опубликовано за ПРОШЛУЮ ISO-неделю
    (по префиксу даты в имени published/<YYYY-MM-DD>-*.md). None → строка про
    посты в дайджесте не рендерится (обратная совместимость).
    """
    week_start: dt.date
    snapshots: list[ChannelSnapshot]
    prev_snapshots: list[ChannelSnapshot]
    posts_published: int | None = None
    generated_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
