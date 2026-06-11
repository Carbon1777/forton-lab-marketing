"""Word-based monthly per-app рендер.

Идентичен hybrid_report.render по стилю (plain text, без эмодзи, без %).
Отличия:
  - Заголовок: «App — отчёт за [месяц] [год]»
  - Блок 1 (сторы): реальные установки за месяц (ASC Analytics + GPlay CSV)
    + честная фраза про RuStore (Mail.ru не отдаёт installs через API)
  - Блок 8: MoM вместо WoW
"""
from __future__ import annotations

import datetime as dt

from src.store_metrics.models import WeekDelta
from src.hybrid_report.models import ProductReport

_MONTHS_RU_NOM = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 100 in (11, 12, 13, 14):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def _pct_word(part: int | None, whole: int | None) -> str | None:
    if part is None or whole is None or whole == 0:
        return None
    pct = round(part / whole * 100)
    word = _plural(pct, "процент", "процента", "процентов")
    return f"{pct} {word}"


def _month_label(d: dt.date) -> str:
    return f"{_MONTHS_RU_NOM[d.month]} {d.year}"


_STORE_ORDER = [
    ("app_store", "App Store"),
    ("google_play", "Google Play"),
    ("rustore", "RuStore"),
]

_RUSTORE_LIMITATION_PHRASE = (
    "RuStore не отдаёт установки через API (ограничение Mail.ru)"
)


def _block_stores(r: ProductReport) -> str:
    """Установки за месяц по сторам — word-based, без эмодзи и знака %.

    RuStore без числа → честная фраза-констрейнт (не «нет данных»). Если
    RuStore вдруг даст число (mock-режим / будущий API) — рендерим число.
    Суффикс «(без RuStore)» у итога — только когда RuStore без числа.
    """
    if not r.store_snaps:
        return "Скачивания по сторам: данные собираются."
    by_store = {s.store: s for s in r.store_snaps}
    parts: list[str] = []
    total = 0
    have_any = False
    rustore_counted = False
    for store_key, label in _STORE_ORDER:
        snap = by_store.get(store_key)
        installs = snap.installs if snap else None
        if installs is None:
            if store_key == "rustore":
                parts.append(_RUSTORE_LIMITATION_PHRASE)
            else:
                parts.append(f"{label} — нет данных")
        else:
            parts.append(f"{label} — {installs}")
            total += installs
            have_any = True
            if store_key == "rustore":
                rustore_counted = True
    line = "Скачивания по сторам: " + ", ".join(parts) + "."
    if have_any:
        suffix = "" if rustore_counted else " (без RuStore)"
        line += f" Всего за месяц {total}{suffix}."
    ratings: list[str] = []
    for store_key, label in _STORE_ORDER:
        snap = by_store.get(store_key)
        if snap and snap.rating is not None:
            ratings.append(f"{label} {round(snap.rating, 1)}")
    if ratings:
        line += " Рейтинг: " + ", ".join(ratings) + "."
    return line


def _block_am_installs(r: ProductReport) -> str:
    if r.am_installs_total is None:
        return "Установки по данным AppMetrica: данные собираются."
    line = f"Установки по данным AppMetrica: {r.am_installs_total}."
    org_pct = _pct_word(r.am_installs_organic, r.am_installs_total)
    if r.am_installs_organic is not None:
        suffix = f" ({org_pct})" if org_pct else ""
        line += f" Органические — {r.am_installs_organic}{suffix}."
    if r.am_installs_ads:
        ads_pct = _pct_word(r.am_installs_ads, r.am_installs_total)
        suffix = f" ({ads_pct})" if ads_pct else ""
        src = r.am_ads_publisher or "реклама"
        line += f" Из рекламы {src} — {r.am_installs_ads}{suffix}."
    line += " Цифры сторов и AppMetrica считаются по-разному и не складываются."
    return line


def _block_activity(r: ProductReport) -> str:
    a = r.activity
    if a.sessions is None and a.active_users is None and a.avg_session_sec is None:
        return "Активность: данные собираются."
    sessions = a.sessions or 0
    users = a.active_users or 0
    avg = int(round(a.avg_session_sec or 0))
    s_word = _plural(sessions, "сессия", "сессии", "сессий")
    u_word = _plural(
        users, "активный пользователь",
        "активных пользователя", "активных пользователей",
    )
    sec_word = _plural(avg, "секунда", "секунды", "секунд")
    return (
        f"Активность: {sessions} {s_word}, {users} {u_word}, "
        f"средняя сессия {avg} {sec_word}."
    )


def _block_funnel(r: ProductReport) -> str:
    f = r.funnel
    if f.error or len(f.steps) < 2:
        return "Воронка онбординга: данные собираются."
    pieces: list[str] = []
    max_drop_pct = -1.0
    max_drop_label = ""
    first_devices = f.steps[0].devices
    prev_devices: int | None = None
    for step in f.steps:
        if prev_devices is None:
            pieces.append(f"{step.label} — {step.devices}")
        else:
            conv = _pct_word(step.devices, first_devices)
            conv_part = f" ({conv} от открывших приложение)" if conv else ""
            pieces.append(f"{step.label} — {step.devices}{conv_part}")
            if prev_devices > 0:
                drop = (prev_devices - step.devices) / prev_devices * 100
                if drop > max_drop_pct:
                    max_drop_pct = drop
                    max_drop_label = step.label
        prev_devices = step.devices
    line = "Воронка онбординга (сколько дошло до шага): " + "; ".join(pieces) + "."
    if max_drop_label and max_drop_pct > 0:
        pct = round(max_drop_pct)
        word = _plural(pct, "процент", "процента", "процентов")
        line += (
            f" Наибольший отвал — на шаге {max_drop_label}: "
            f"теряется {pct} {word}."
        )
    return line


def _block_screens(r: ProductReport) -> str:
    s = r.screens
    if s.error or not s.screens:
        return "Экраны (топ по заходам): данные собираются."
    names = r.spec.screen_names
    pieces = [f"{names.get(sc.name, sc.name)} — {sc.views}" for sc in s.screens]
    return "Экраны (топ по заходам): " + "; ".join(pieces) + "."


def _block_reg(r: ProductReport) -> str:
    reg = r.reg
    if reg.registrations is None and reg.activations is None:
        return "Регистрации и активация: данные собираются."
    regs = reg.registrations
    acts = reg.activations
    if regs is None:
        return f"Регистрации и активация: активировались {acts}."
    if acts is None:
        return f"Регистрации и активация: зарегистрировались {regs}."
    pct = _pct_word(acts, regs)
    suffix = f" ({pct})" if pct else ""
    return (
        f"Регистрации и активация: зарегистрировались {regs}, "
        f"активировались {acts}{suffix}."
    )


def _block_retention() -> str:
    return "Удержание (1, 7 и 30 дней): пока недоступно, метрика готовится."


def _block_mom(r: ProductReport) -> str:
    curr = r.am_installs_total
    prev = r.prev_am_installs_total
    if prev is None or curr is None:
        return (
            "По сравнению с предыдущим месяцем: прошлый месяц "
            "недоступен для сравнения."
        )
    delta = WeekDelta.compute(curr, prev)
    if prev == curr:
        return (
            f"По сравнению с предыдущим месяцем установок столько же (было {prev})."
        )
    if delta.delta_pct is None:
        return (
            f"По сравнению с предыдущим месяцем установок стало больше "
            f"(было {prev})."
        )
    pct = abs(round(delta.delta_pct))
    word = _plural(pct, "процент", "процента", "процентов")
    direction = "больше" if curr > prev else "меньше"
    return (
        f"По сравнению с предыдущим месяцем установок {direction} на "
        f"{pct} {word} (было {prev})."
    )


def render_monthly_report(report: ProductReport) -> str:
    """Ежемесячный отчёт — word-based plain-text на один продукт."""
    title = f"{report.spec.display} — отчёт за {_month_label(report.week_start)}"
    lines = [
        title,
        _block_stores(report),
        _block_am_installs(report),
        _block_activity(report),
        _block_funnel(report),
        _block_screens(report),
        _block_reg(report),
        _block_retention(),
        _block_mom(report),
    ]
    return "\n".join(lines)
