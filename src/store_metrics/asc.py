"""Apple App Store Connect — Analytics Reports API adapter (JWT ES256).

Pipeline:
    1. Generate JWT (PyJWT ES256) с claims iss=Issuer ID, aud=appstoreconnect-v1, exp≤20min
    2. POST /v1/analyticsReportRequests с accessType=ONE_TIME_SNAPSHOT для каждого product
    3. GET /v1/analyticsReportRequests/{id}/reports — список доступных отчётов
    4. GET /v1/analyticsReports/{id}/instances?filter[granularity]=WEEKLY
    5. GET /v1/analyticsReportInstances/{id}/segments → download CSV
    6. Parse CSV → installs/uninstalls/rating per product

Env required:
    ASC_KEY_ID         — 10-char Key ID
    ASC_ISSUER_ID      — UUID команды
    ASC_PRIVATE_KEY    — content .p8 file (multi-line)
    ASC_APP_ID_CENTRY  — app id из URL appstoreconnect.apple.com/apps/<id>
    ASC_APP_ID_DIKTUM  — same для Diktum

Current state: SKELETON — fetch_weekly возвращает mock-данные пока secrets
не загружены.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Final

from .models import Product, StoreSnapshot

_ASC_BASE: Final[str] = "https://api.appstoreconnect.apple.com/v1"

_MOCK_INSTALLS: dict[Product, int] = {"centry": 23, "diktum": 18}
_MOCK_PREV: dict[Product, int] = {"centry": 19, "diktum": 22}


def _is_configured() -> bool:
    keys = ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY",
            "ASC_APP_ID_CENTRY", "ASC_APP_ID_DIKTUM")
    return all(os.environ.get(k) for k in keys)


def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    if not _is_configured():
        return StoreSnapshot(
            product=product, store="app_store", week_start=week_start,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.7 if product == "centry" else 4.6,
            top_country="RU", top_country_share=0.78,
        )
    raise NotImplementedError(
        "ASC secrets configured but real implementation not yet wired."
    )


def fetch_previous(product: Product, week_start: dt.date) -> StoreSnapshot:
    if not _is_configured():
        return StoreSnapshot(
            product=product, store="app_store",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.7 if product == "centry" else 4.5,
            top_country="RU", top_country_share=0.75,
        )
    raise NotImplementedError
