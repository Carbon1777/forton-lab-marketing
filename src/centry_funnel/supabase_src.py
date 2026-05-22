"""Supabase RPC get_centry_funnel_metrics — профили/гости/USER/активация.

Вызывает SECURITY DEFINER функцию через PostgREST с service-role ключом Centry.
Env: SUPABASE_URL_CENTRY, SUPABASE_SERVICE_ROLE_KEY_CENTRY.
RPC возвращает массив из одной строки.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from src.store_metrics._http import fetch_with_retry


@dataclass(frozen=True)
class FunnelDB:
    new_profiles: int
    guests: int
    users: int
    activations: int


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} missing")
    return v


def fetch_funnel(
    week_start: dt.date,
    week_end: dt.date,
    url: str | None = None,
    key: str | None = None,
) -> FunnelDB:
    """Воронка Centry за период через RPC get_centry_funnel_metrics."""
    url = url or _env("SUPABASE_URL_CENTRY")
    key = key or _env("SUPABASE_SERVICE_ROLE_KEY_CENTRY")
    resp = fetch_with_retry(
        f"{url}/rest/v1/rpc/get_centry_funnel_metrics",
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
    if not rows:
        return FunnelDB(new_profiles=0, guests=0, users=0, activations=0)
    r = rows[0]
    return FunnelDB(
        new_profiles=int(r["new_profiles"]),
        guests=int(r["guests"]),
        users=int(r["users"]),
        activations=int(r["activations"]),
    )
