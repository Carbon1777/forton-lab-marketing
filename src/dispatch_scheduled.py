"""Запуск отложенных публикаций (видео-посты с ``publish_at_msk``).

preview_bot на «Публикуй» ставит запись плана в статус ``scheduled``
(см. ``_handle_publish``). Этот модуль запускается воркфлоу
``publish_scheduled.yml`` по крону Cloudflare Worker (20:00 МСК): находит
записи плана со статусом ``scheduled`` и датой <= сегодня (по Москве) и
дёргает ``publish.yml`` для каждой — тем же путём, что и обычное одобрение.

Статус ``published`` после успешной публикации ставит сам ``publish.yml``.
Если запись осталась ``scheduled`` (публикация упала), следующий запуск
повторит попытку — поэтому «дата <= сегодня», а не «== сегодня».
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from src.plan_reader import PlanEntry, discover_plans, parse_plan
from src.plan_writer import dispatch_publish

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = REPO_ROOT / "plans"


def _today_msk() -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).date()


def find_due(plans_dir: Path, today: dt.date) -> list[PlanEntry]:
    """Записи со статусом ``scheduled`` и датой <= ``today`` (из планов месяца и прошлого)."""
    due: list[PlanEntry] = []
    for plan_path in discover_plans(plans_dir):
        month = plan_path.stem.removeprefix("monthly_plan_")
        # планы будущих месяцев не читаем
        if month > today.strftime("%Y-%m"):
            continue
        plan = parse_plan(plan_path)
        due.extend(
            e for e in plan.entries
            if e.status == "scheduled" and e.date <= today
        )
    return sorted(due, key=lambda e: (e.date, e.slug))


def main() -> int:
    today = _today_msk()
    due = find_due(PLANS_DIR, today)
    if not due:
        print(f"{today.isoformat()}: нет отложенных публикаций")
        return 0
    failures = 0
    for entry in due:
        try:
            dispatch_publish(entry.slug)
            print(f"dispatched publish.yml slug={entry.slug} (план {entry.date.isoformat()})")
        except Exception as exc:  # GitHubAPIError / KeyError(BOT_DISPATCH_PAT)
            sys.stderr.write(f"FAIL {entry.slug}: {type(exc).__name__}: {exc}\n")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
