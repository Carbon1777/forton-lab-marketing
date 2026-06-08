"""Per-product сбор всех источников → ProductReport.

КОНТРАКТ: never-raises per источник. Каждый источник в своём try/except;
ошибка одного → None/error в модель, остальные блоки собираются. Сообщение
по продукту уходит всегда (мягкая деградация секций в render).

Переиспользует импортом:
  - store_metrics.{asc,play,rustore}.fetch_weekly(product, week_start)
  - centry_funnel.appmetrica / diktum_funnel.appmetrica.fetch_installs (ym:ts:*)
  - centry_funnel.supabase_src.fetch_funnel / diktum_funnel.supabase_src.fetch_registrations

Семантика регистраций (решение по флагу плана):
  - Centry RPC get_centry_funnel_metrics → (new_profiles, guests, users,
    activations). «зарегистрировались» = users (state=USER, завершили
    регистрацию), «активировались» = activations. НЕ new_profiles (это все
    созданные профили включая гостей).
  - Diktum RPC get_funnel_metrics → (registrations, activated) напрямую.
"""
from __future__ import annotations

import datetime as dt
import sys

from src.store_metrics import asc, play, rustore
from src.store_metrics.models import StoreSnapshot
from src.centry_funnel import supabase_src as centry_db
from src.diktum_funnel import supabase_src as diktum_db

from . import appmetrica
from .models import (
    AppMetricaActivity,
    ProductReport,
    ProductSpec,
    RegActivation,
)

# Имя стора → (модуль) — функцию fetch_weekly берём по имени в момент вызова,
# чтобы patch.object(gather.<mod>, "fetch_weekly") работал в тестах (а не
# захватывать ссылку на функцию на import-time).
_STORE_MODULES = [
    ("app_store", asc),
    ("google_play", play),
    ("rustore", rustore),
]

_ORGANIC_NAME = "Органика"


def _collect_stores(
    product_key: str, week_start: dt.date
) -> tuple[list[StoreSnapshot], str | None]:
    """Каждый стор в своём try. error — если ВСЕ упали."""
    snaps: list[StoreSnapshot] = []
    failures = 0
    for store_name, module in _STORE_MODULES:
        try:
            snaps.append(module.fetch_weekly(product_key, week_start))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            sys.stderr.write(
                f"WARN: store {store_name} failed: {type(exc).__name__}: "
                f"{str(exc)[:80]}\n"
            )
            snaps.append(StoreSnapshot(
                product=product_key,  # type: ignore[arg-type]
                store=store_name,  # type: ignore[arg-type]
                week_start=week_start, installs=None,
                error=f"{type(exc).__name__}: {str(exc)[:80]}",
            ))
    store_error = "all stores failed" if failures == len(_STORE_MODULES) else None
    return snaps, store_error


def _ads_publisher(by_publisher: dict[str, int]) -> str | None:
    """Первый ключ ≠ «Органика» (имя рекламного источника), иначе None."""
    for name in by_publisher:
        if name != _ORGANIC_NAME:
            return name
    return None


def _collect_installs(spec: ProductSpec, week_start: dt.date, week_end: dt.date):
    """AppMetrica installs (ym:ts:*) по appmetrica_app_id продукта — generic."""
    try:
        inst = appmetrica.fetch_installs(
            spec.appmetrica_app_id, week_start, week_end
        )
        return (
            inst.total, inst.organic, inst.ads,
            _ads_publisher(inst.by_publisher), None,
        )
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: AppMetrica installs failed: {err}\n")
        return None, None, None, None, err


def _collect_reg(spec: ProductSpec, week_start: dt.date, week_end: dt.date) -> RegActivation:
    """Supabase регистрации → активация. Семантика per продукт (см. модуль-doc).

    Только Centry/Diktum имеют Supabase-RPC. Новые продукты (reg_source не
    centry/diktum) → None: их RPC ещё нет, Лапуля вообще без сервера. Render
    покажет «данные собираются»; рег/актив видна в воронке онбординга AppMetrica.
    """
    try:
        if spec.reg_source == "centry":
            db = centry_db.fetch_funnel(week_start, week_end)
            # registrations = users (завершившие регистрацию, state=USER)
            return RegActivation(registrations=db.users, activations=db.activations)
        if spec.reg_source == "diktum":
            db = diktum_db.fetch_registrations(week_start, week_end)
            return RegActivation(registrations=db.registrations, activations=db.activated)
        return RegActivation(registrations=None, activations=None)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"WARN: Supabase reg failed: {type(exc).__name__}: {str(exc)[:80]}\n"
        )
        return RegActivation(registrations=None, activations=None)


def _collect_activity(spec: ProductSpec, week_start: dt.date, week_end: dt.date) -> AppMetricaActivity:
    """fetch_activity делает raise_for_status — оборачиваем в try (never-raises)."""
    try:
        return appmetrica.fetch_activity(spec.appmetrica_app_id, week_start, week_end)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"WARN: AppMetrica activity failed: {type(exc).__name__}: "
            f"{str(exc)[:80]}\n"
        )
        return AppMetricaActivity(sessions=None, active_users=None,
                                  avg_session_sec=None)


def gather_product(
    spec: ProductSpec,
    week_start: dt.date,
    week_end: dt.date,
    snapshots_data: dict,
) -> ProductReport:
    """Собрать ВСЕ источники для одного продукта. Никогда не падает на одном
    сбойном источнике — его блок деградирует, остальные живут."""
    from . import snapshot  # локальный импорт чтобы не плодить циклы

    store_snaps, store_error = _collect_stores(spec.key, week_start)

    am_total, am_organic, am_ads, am_pub, am_err = _collect_installs(
        spec, week_start, week_end
    )

    activity = _collect_activity(spec, week_start, week_end)
    # funnel/screens уже never-raises (Task 3 возвращает error-датаклассы)
    funnel = appmetrica.fetch_onboarding_funnel(
        spec.appmetrica_app_id, spec.onboarding_steps, week_start, week_end
    )
    screens = appmetrica.fetch_top_screens(
        spec.appmetrica_app_id, week_start, week_end,
        event_label=spec.screen_event_label,
    )

    reg = _collect_reg(spec, week_start, week_end)

    prev = snapshot.get_prev_installs(snapshots_data, week_start, spec.key)

    return ProductReport(
        spec=spec,
        week_start=week_start,
        week_end=week_end,
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
        prev_am_installs_total=prev,
    )
