"""Entrypoint — ежемесячный per-app отчёт.

5-го числа каждого месяца отправляет в TG-канал «Планировщик» отчёт за
предыдущий полный месяц для каждого продукта студии (Centry, Diktum, Lucea,
Лапуля, Unia, Листвия — источник истины PRODUCTS) с MoM-сравнением.

Поддерживает --dry-run (или env MONTHLY_DRY_RUN=1): вывод в STDOUT без TG,
снапшот НЕ пишется. Вызывается из .github/workflows/monthly_report.yml.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Final

import requests

from src.hybrid_report import appmetrica
from src.hybrid_report.models import (
    PRODUCTS,
    AppMetricaActivity,
    AppMetricaReviewPrompts,
    ProductReport,
    RegActivation,
)
from src.store_metrics import asc, play, rustore
from src.store_metrics.models import StoreSnapshot
from src.centry_funnel import supabase_src as centry_db
from src.diktum_funnel import supabase_src as diktum_db
from . import snapshot
from .render import render_monthly_report

SNAPSHOTS_PATH: Final[Path] = Path(".metrics/monthly_snapshots.json")

_ORGANIC_NAME = "Органика"

# Имя стора → модуль — функцию fetch_monthly берём по атрибуту в момент
# вызова, чтобы patch.object(cli.<mod>, "fetch_monthly") работал в тестах
# (паттерн hybrid_report/gather.py::_STORE_MODULES).
_STORE_MODULES = [
    ("app_store", asc),
    ("google_play", play),
    ("rustore", rustore),
]


def _collect_stores(
    product_key: str, month_start: dt.date
) -> tuple[list[StoreSnapshot], str | None]:
    """Каждый стор в своём try. error — если ВСЕ упали.

    Копия hybrid_report/gather.py::_collect_stores, но месячный контур:
    module.fetch_monthly(product, year, month). Недельный gather не трогаем
    (изоляция контуров).
    """
    snaps: list[StoreSnapshot] = []
    failures = 0
    for store_name, module in _STORE_MODULES:
        try:
            snaps.append(module.fetch_monthly(
                product_key, month_start.year, month_start.month,
            ))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            sys.stderr.write(
                f"WARN: store {store_name} monthly failed: "
                f"{type(exc).__name__}: {str(exc)[:80]}\n"
            )
            snaps.append(StoreSnapshot(
                product=product_key,  # type: ignore[arg-type]
                store=store_name,  # type: ignore[arg-type]
                week_start=month_start, installs=None,
                error=f"{type(exc).__name__}: {str(exc)[:80]}",
            ))
    store_error = "all stores failed" if failures == len(_STORE_MODULES) else None
    return snaps, store_error


def _report_month(today: dt.date) -> tuple[dt.date, dt.date]:
    """Предыдущий полный месяц: (первое_число, последнее_число)."""
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - dt.timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev, last_of_prev


def _collect_installs_by_store(spec, start: dt.date, end: dt.date):
    """AppMetrica разбивка установок по магазинам (ym:ts:appInstaller) за месяц.

    Надёжный источник стор-блока вместо хрупких ASC/Play/RuStore. never-raises:
    ошибка → ([], error), render деградирует мягко. fetch_installs_by_store
    generic по датам — месячный диапазон работает без изменений.
    """
    try:
        res = appmetrica.fetch_installs_by_store(spec.appmetrica_app_id, start, end)
        return res.rows, None
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: AppMetrica monthly installs-by-store failed: {err}\n")
        return [], err


def _collect_installs(spec, start: dt.date, end: dt.date):
    try:
        inst = appmetrica.fetch_installs(spec.appmetrica_app_id, start, end)
        ads_publisher = next(
            (name for name in inst.by_publisher if name != _ORGANIC_NAME), None
        )
        return inst.total, inst.organic, inst.ads, ads_publisher, None
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: AppMetrica monthly installs failed: {err}\n")
        return None, None, None, None, err


def _collect_activity(spec, start: dt.date, end: dt.date) -> AppMetricaActivity:
    try:
        return appmetrica.fetch_activity(spec.appmetrica_app_id, start, end)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"WARN: AppMetrica monthly activity failed: {type(exc).__name__}: "
            f"{str(exc)[:80]}\n"
        )
        return AppMetricaActivity(sessions=None, active_users=None, avg_session_sec=None)


def _collect_reg(spec, start: dt.date, end: dt.date) -> RegActivation:
    try:
        if spec.reg_source == "centry":
            db = centry_db.fetch_funnel(start, end)
            return RegActivation(registrations=db.users, activations=db.activations)
        if spec.reg_source == "diktum":
            db = diktum_db.fetch_registrations(start, end)
            return RegActivation(registrations=db.registrations, activations=db.activated)
        return RegActivation(registrations=None, activations=None)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"WARN: Supabase monthly reg failed: {type(exc).__name__}: "
            f"{str(exc)[:80]}\n"
        )
        return RegActivation(registrations=None, activations=None)


def _gather_product_monthly(spec, month_start: dt.date, month_end: dt.date, data: dict) -> ProductReport:
    """Собрать все источники за месяц. Never-raises per источник."""
    # Витрина стор-блока — AppMetrica appInstaller (надёжно). ASC/Play/RuStore
    # ещё собираем для рейтингов + опц. сверки, но они больше не источник чисел.
    am_by_store, am_store_err = _collect_installs_by_store(
        spec, month_start, month_end
    )
    store_snaps, store_error = _collect_stores(spec.key, month_start)
    am_total, am_organic, am_ads, am_pub, am_err = _collect_installs(
        spec, month_start, month_end
    )
    activity = _collect_activity(spec, month_start, month_end)
    funnel = appmetrica.fetch_onboarding_funnel(
        spec.appmetrica_app_id, spec.onboarding_steps, month_start, month_end
    )
    screens = appmetrica.fetch_top_screens(
        spec.appmetrica_app_id, month_start, month_end,
        event_label=spec.screen_event_label,
    )
    reg = _collect_reg(spec, month_start, month_end)
    if spec.review_event:
        review_prompts = appmetrica.fetch_review_prompts(
            spec.appmetrica_app_id, spec.review_event, month_start, month_end
        )
    else:
        review_prompts = AppMetricaReviewPrompts(available=False)
    prev = snapshot.get_prev_installs(
        data, month_start.year, month_start.month, spec.key
    )
    return ProductReport(
        spec=spec,
        week_start=month_start,
        week_end=month_end,
        am_installs_by_store=am_by_store,
        am_store_error=am_store_err,
        store_snaps=store_snaps,
        store_error=store_error,
        am_installs_total=am_total,
        am_installs_organic=am_organic,
        am_installs_ads=am_ads,
        am_ads_publisher=am_pub,
        am_installs_error=am_err,
        activity=activity,
        funnel=funnel,
        screens=screens,
        reg=reg,
        review_prompts=review_prompts,
        prev_am_installs_total=prev,
    )


def send_to_planner(text: str) -> bool:
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — сообщение не отправлено\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        sys.stderr.write(f"ERROR: TG HTTP {r.status_code}: {r.text[:200]}\n")
        return False
    except requests.RequestException as exc:
        sys.stderr.write(f"ERROR: TG send failed: {exc!r}\n")
        return False


def _resolve_dry_run(dry_run: bool | None) -> bool:
    if dry_run is not None:
        return dry_run
    if "--dry-run" in sys.argv:
        return True
    return os.environ.get("MONTHLY_DRY_RUN") == "1"


def main(
    today: dt.date | None = None,
    snapshots_path: Path | None = None,
    dry_run: bool | None = None,
) -> int:
    today = today or dt.date.today()
    snapshots_path = snapshots_path or SNAPSHOTS_PATH
    dry = _resolve_dry_run(dry_run)

    month_start, month_end = _report_month(today)
    sys.stderr.write(
        f"INFO: monthly report for {month_start}–{month_end} "
        f"(dry_run={dry})\n"
    )

    data = snapshot.load(snapshots_path)
    ok = True
    for spec in PRODUCTS:
        report = _gather_product_monthly(spec, month_start, month_end, data)
        text = render_monthly_report(report)
        print(text)
        print()
        if not dry:
            ok = send_to_planner(text) and ok
        snapshot.store_month(
            data, month_start.year, month_start.month, spec.key, report.am_installs_total
        )

    if not dry:
        snapshot.save(snapshots_path, data)

    return 0 if (ok or dry) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
