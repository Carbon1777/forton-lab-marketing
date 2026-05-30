"""Word-based per-app рендер гибридного отчёта.

HARD requirement (утверждён пользователем): ВСЁ СЛОВАМИ, БЕЗ эмодзи / стрелок /
значков / знака процента. Проценты — слово «процентов» (с правильным
склонением). Plain text (НЕ HTML): никаких <b>, маркеров, ссылок.

8 блоков на сообщение:
  1. Скачивания по сторам + рейтинг + всего.
  2. Установки AppMetrica (органика / реклама) + примечание о несложении.
  3. Активность (сессии / активные / ср. сессия).
  4. Воронка онбординга + явный шаг макс. отвала.
  5. Экраны (топ по заходам).
  6. Регистрации → активация.
  7. Удержание — заглушка (метрика недоступна).
  8. Сравнение с прошлой неделей (WoW) словами.

Мягкая деградация: при ошибке/отсутствии источника секция показывает
«данные собираются», остальное сообщение живёт.
"""
from __future__ import annotations

import datetime as dt

from src.store_metrics.models import WeekDelta
from .models import ProductReport

_MONTHS_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

_STORE_ORDER = [
    ("app_store", "App Store"),
    ("google_play", "Google Play"),
    ("rustore", "RuStore"),
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу (1 / 2-4 / 5+, с учётом 11-14)."""
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
    """«X процентов» (с правильным склонением) или None если посчитать нельзя."""
    if part is None or whole is None or whole == 0:
        return None
    pct = round(part / whole * 100)
    word = _plural(pct, "процент", "процента", "процентов")
    return f"{pct} {word}"


def _date_range(week_start: dt.date, week_end: dt.date) -> str:
    s, e = week_start, week_end
    if s.month == e.month:
        return f"{s.day}–{e.day} {_MONTHS_RU[e.month]}"
    return f"{s.day} {_MONTHS_RU[s.month]} – {e.day} {_MONTHS_RU[e.month]}"


def _block_stores(r: ProductReport) -> str:
    by_store = {s.store: s for s in r.store_snaps}
    parts: list[str] = []
    total = 0
    have_any = False
    for store_key, label in _STORE_ORDER:
        snap = by_store.get(store_key)
        installs = snap.installs if snap else None
        if installs is None:
            parts.append(f"{label} — нет данных")
        else:
            parts.append(f"{label} — {installs}")
            total += installs
            have_any = True
    line = "Скачивания из сторов: " + ", ".join(parts) + "."
    if have_any:
        line += f" Всего {total}."
    # рейтинги — если есть, добавить словами (опционально)
    ratings: list[str] = []
    for store_key, label in _STORE_ORDER:
        snap = by_store.get(store_key)
        if snap and snap.rating is not None:
            ratings.append(f"рейтинг {label} {snap.rating}")
    if ratings:
        line += " " + "; ".join(ratings) + "."
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
    line += (
        " Цифры сторов и AppMetrica считаются по-разному и не складываются."
    )
    return line


def _block_activity(r: ProductReport) -> str:
    a = r.activity
    if a.sessions is None and a.active_users is None and a.avg_session_sec is None:
        return "Активность: данные собираются."
    sessions = a.sessions or 0
    users = a.active_users or 0
    avg = int(round(a.avg_session_sec or 0))
    s_word = _plural(sessions, "сессия", "сессии", "сессий")
    u_word = _plural(users, "активный пользователь",
                     "активных пользователя", "активных пользователей")
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
            # первый шаг — только число, он и есть знаменатель конверсии
            pieces.append(f"{step.label} — {step.devices}")
        else:
            # конверсия от ПЕРВОГО шага («от открывших приложение»):
            # эти события не строгая последовательная воронка, поэтому
            # «от предыдущего» давало бы >100%. От первого — всегда ≤100%.
            conv = _pct_word(step.devices, first_devices)
            conv_part = f" ({conv} от открывших приложение)" if conv else ""
            pieces.append(f"{step.label} — {step.devices}{conv_part}")
            # шаг макс. отвала — наибольшая ПОСЛЕДОВАТЕЛЬНАЯ относительная
            # потеря (prev→cur), она верно находит реальный провал.
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
    pieces = [f"{sc.name} — {sc.views}" for sc in s.screens]
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


def _block_wow(r: ProductReport) -> str:
    curr = r.am_installs_total
    prev = r.prev_am_installs_total
    if prev is None or curr is None:
        return (
            "По сравнению с прошлой неделей: прошлая неделя недоступна "
            "для сравнения."
        )
    delta = WeekDelta.compute(curr, prev)
    if prev == curr:
        return f"По сравнению с прошлой неделей установок столько же (было {prev})."
    if delta.delta_pct is None:
        # prev == 0, curr > 0 — рост с нуля
        return (
            f"По сравнению с прошлой неделей установок стало больше "
            f"(было {prev})."
        )
    pct = abs(round(delta.delta_pct))
    word = _plural(pct, "процент", "процента", "процентов")
    direction = "больше" if curr > prev else "меньше"
    return (
        f"По сравнению с прошлой неделей установок {direction} на "
        f"{pct} {word} (было {prev})."
    )


def render_report(report: ProductReport) -> str:
    """Собрать word-based plain-text сообщение для одного продукта."""
    title = (
        f"{report.spec.display} — отчёт за неделю "
        f"{_date_range(report.week_start, report.week_end)}"
    )
    lines = [
        title,
        _block_stores(report),
        _block_am_installs(report),
        _block_activity(report),
        _block_funnel(report),
        _block_screens(report),
        _block_reg(report),
        _block_retention(),
        _block_wow(report),
    ]
    return "\n".join(lines)
