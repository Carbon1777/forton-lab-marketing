"""Domain model — одна неделя воронки Centry (4 ступени)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class FunnelWeek:
    """Воронка Centry за одну ISO-неделю (Пн–Вс), TZ Europe/Moscow.

    None в installs_* — AppMetrica недоступна (см. appmetrica_error);
    None в new_profiles/guests/users/activations — Supabase недоступен
    (supabase_error). Digest рендерит «—» для None.

    Семантика (cold-start исключён):
      new_profiles — реальные app_users, созданные в неделе;
      guests/users — из них state=GUEST / state=USER (guests+users=new_profiles);
      activations  — реальные юзеры, чьё первое членство в плане — в этой неделе.
    """
    week_start: dt.date
    week_end: dt.date
    installs_total: int | None
    installs_organic: int | None
    installs_ads: int | None
    new_profiles: int | None
    guests: int | None
    users: int | None
    activations: int | None
    appmetrica_error: str | None = None
    supabase_error: str | None = None
