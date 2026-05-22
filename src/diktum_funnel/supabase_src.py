"""Supabase RPC get_funnel_metrics — регистрации + активация по неделе.

Вызывает SECURITY DEFINER функцию через PostgREST с service-role ключом.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from src.store_metrics._http import fetch_with_retry


@dataclass(frozen=True)
class FunnelDB:
    registrations: int
    activated: int


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} missing")
    return v


def fetch_registrations(
    week_start: dt.date,
    week_end: dt.date,
    url: str | None = None,
    key: str | None = None,
) -> FunnelDB:
    """Сумма регистраций и активаций за период через RPC."""
    url = url or _env("SUPABASE_URL")
    key = key or _env("SUPABASE_SERVICE_ROLE_KEY")
    resp = fetch_with_retry(
        f"{url}/rest/v1/rpc/get_funnel_metrics",
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json_body={"p_from": week_start.isoformat(), "p_to": week_end.isoformat()},
    )
    resp.raise_for_status()
    rows = resp.json()
    registrations = sum(int(r["registrations"]) for r in rows)
    activated = sum(int(r["activated"]) for r in rows)
    return FunnelDB(registrations=registrations, activated=activated)
