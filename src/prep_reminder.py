"""prep_reminder.py — шлёт TG-нудж 21 числа каждого месяца.

Дёргается из workflow `monthly_prep_reminder.yml` (cron 0 9 21 * * UTC = 12 МСК
21 числа). Напоминает юзеру что пора готовить контент на следующий месяц.

Use:
    PYTHONPATH=. python -m src.prep_reminder

Env: TG_PLANNER_BOT_TOKEN, TG_OWNER_CHAT_ID
"""
from __future__ import annotations

import datetime as dt
import sys

from src import tg_nudge

RU_MONTHS = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}
RU_MONTHS_GENITIVE = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def next_month_name() -> str:
    today = dt.date.today()
    nxt = today.replace(day=1) + dt.timedelta(days=35)
    nxt = nxt.replace(day=1)
    return f"{RU_MONTHS[nxt.month]} {nxt.year}"


def main() -> int:
    next_month = next_month_name()
    today = dt.date.today()
    days_left = (today.replace(day=1) + dt.timedelta(days=35)).replace(day=1) - today
    try:
        tg_nudge.send(
            "monthly_prep_due",
            next_month=next_month,
            days_left=days_left.days,
            today_str=f"{today.day} {RU_MONTHS_GENITIVE[today.month]}",
        )
        print(f"OK: prep reminder sent for {next_month}")
        return 0
    except Exception as exc:
        sys.stderr.write(f"ERROR: prep reminder failed: {exc!r}\n")
        return 0   # не блокируем cron, просто логируем


if __name__ == "__main__":
    sys.exit(main())
