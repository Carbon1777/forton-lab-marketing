"""Domain model — одна неделя воронки Diktum."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class FunnelWeek:
    """Воронка за одну ISO-неделю (Пн–Вс), TZ Europe/Moscow.

    None в installs_* означает, что AppMetrica недоступна (см. appmetrica_error);
    None в registrations/activated — что Supabase недоступен (supabase_error).
    Digest рендерит «—» для None.
    """
    week_start: dt.date
    week_end: dt.date
    installs_total: int | None
    installs_organic: int | None
    installs_ads: int | None
    registrations: int | None
    activated: int | None
    appmetrica_error: str | None = None
    supabase_error: str | None = None
