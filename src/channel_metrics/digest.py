"""HTML-дайджест охватов каналов для TG-канала «Планировщик» (parse_mode=HTML).

Формат:
    📡 Forton Lab — каналы, неделя 30.06-06.07.2026

    🔵 Telegram   1 234 подписчиков  (+12 📈)
    🟦 VK           567 подписчиков  (+3 📈)   охват: 8 900
    🔴 YouTube      — (нет данных: optional)

    <i>Собрано HH:MM МСК. Подписчики — снимок на понедельник.</i>
"""
from __future__ import annotations

import datetime as dt
import html

from .models import (
    PLATFORM_LABEL,
    PLATFORM_ORDER,
    ChannelDelta,
    ChannelReport,
    ChannelSnapshot,
)


def _fmt_thousands(n: int | None) -> str:
    if n is None:
        return "—"
    # Пробел как разделитель тысяч (RU-стиль): 1234 → "1 234".
    return f"{n:,}".replace(",", " ")


def _fmt_delta(delta: ChannelDelta) -> str:
    if delta.delta_abs is None:
        return delta.arrow  # "—"
    sign = "+" if delta.delta_abs >= 0 else ""
    return f"({sign}{delta.delta_abs} {delta.arrow})"


def _render_row(snap: ChannelSnapshot, prev: ChannelSnapshot | None) -> str:
    label = PLATFORM_LABEL.get(snap.platform, snap.platform)
    if snap.subscribers is None:
        note = snap.error or "нет данных"
        return f"{label}   — <i>({html.escape(note)})</i>"
    delta = ChannelDelta.compute(
        snap.subscribers, prev.subscribers if prev else None
    )
    row = f"{label}   {_fmt_thousands(snap.subscribers)} подписчиков  {_fmt_delta(delta)}"
    if snap.reach is not None:
        row += f"   охват: {_fmt_thousands(snap.reach)}"
    return row


def render_channel_digest(report: ChannelReport) -> str:
    """Return HTML digest для tg sendMessage с parse_mode=HTML."""
    week_end = report.week_start + dt.timedelta(days=6)
    lines: list[str] = [
        f"📡 <b>Forton Lab — каналы, неделя "
        f"{report.week_start.strftime('%d.%m')}-{week_end.strftime('%d.%m.%Y')}</b>",
        "",
    ]

    prev_by = {s.platform: s for s in report.prev_snapshots}
    by_platform = {s.platform: s for s in report.snapshots}
    for platform in PLATFORM_ORDER:
        snap = by_platform.get(platform)
        if snap is None:
            continue
        lines.append(_render_row(snap, prev_by.get(platform)))

    lines.append("")
    ts_msk = (report.generated_at + dt.timedelta(hours=3)).strftime("%H:%M")
    lines.append(
        f"<i>Собрано {ts_msk} МСК автоматически. "
        f"Подписчики — снимок; охват VK — за неделю.</i>"
    )
    return "\n".join(lines)
