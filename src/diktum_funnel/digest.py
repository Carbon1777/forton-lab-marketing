"""HTML digest для ТГ-канала «Планировщик» — воронка Diktum.

Формат:
    📊 Diktum — воронка за 12–18 мая

    Установки      18  (орг 16 / реклама 2)  📈 +3
     └ Регистрации  4  (22% от устан.)        📉 −1
        └ Активация 2  (50% от рег · 11%)     →

    Реклама: VK Ads — 2 уст.
"""
from __future__ import annotations

from .models import FunnelWeek
from src.store_metrics.models import WeekDelta

_MONTHS_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _fmt(value: int | None) -> str:
    return "—" if value is None else str(value)


def _pct(part: int | None, whole: int | None) -> str:
    if part is None or whole is None or whole == 0:
        return "—"
    return f"{round(part / whole * 100)}%"


def _delta_str(curr: int | None, prev: int | None) -> str:
    d = WeekDelta.compute(curr, prev)
    if d.arrow == "—" or prev is None:
        return ""
    sign = "+" if (curr or 0) >= (prev or 0) else "−"
    diff = abs((curr or 0) - (prev or 0))
    return f"  {d.arrow} {sign}{diff}"


def _date_range(fw: FunnelWeek) -> str:
    s, e = fw.week_start, fw.week_end
    if s.month == e.month:
        return f"{s.day}–{e.day} {_MONTHS_RU[e.month]}"
    return f"{s.day} {_MONTHS_RU[s.month]} – {e.day} {_MONTHS_RU[e.month]}"


def render_digest(fw: FunnelWeek, prev: dict | None) -> str:
    """Собрать HTML-сообщение воронки. prev — запись прошлой недели или None."""
    p_inst = prev.get("installs_total") if prev else None
    p_reg = prev.get("registrations") if prev else None
    p_act = prev.get("activated") if prev else None

    installs_line = _fmt(fw.installs_total)
    if fw.installs_total is not None:
        installs_line += (
            f"  (орг {_fmt(fw.installs_organic)} / реклама {_fmt(fw.installs_ads)})"
        )
    installs_line += _delta_str(fw.installs_total, p_inst)

    reg_line = (
        f"{_fmt(fw.registrations)}  ({_pct(fw.registrations, fw.installs_total)} от устан.)"
        + _delta_str(fw.registrations, p_reg)
    )

    act_through = _pct(fw.activated, fw.installs_total)
    act_line = (
        f"{_fmt(fw.activated)}  ({_pct(fw.activated, fw.registrations)} от рег · "
        f"{act_through} сквозь)" + _delta_str(fw.activated, p_act)
    )

    lines = [
        f"📊 <b>Diktum — воронка за {_date_range(fw)}</b>",
        "",
        f"Установки      {installs_line}",
        f" └ Регистрации  {reg_line}",
        f"    └ Активация {act_line}",
    ]

    if fw.installs_ads:
        lines += ["", f"Реклама: {_fmt(fw.installs_ads)} уст."]

    if fw.appmetrica_error:
        lines += ["", f"⚠️ AppMetrica: {fw.appmetrica_error}"]
    if fw.supabase_error:
        lines += ["", f"⚠️ Supabase: {fw.supabase_error}"]

    return "\n".join(lines)
