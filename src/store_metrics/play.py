"""Google Play Developer Reporting API — installsCount + crashRate.

Pipeline:
    1. Service account JSON в GOOGLE_PLAY_SA_JSON (env var с raw JSON)
    2. google.oauth2.service_account.Credentials.from_service_account_info
    3. build('playdeveloperreporting', 'v1beta1', credentials=...)
    4. vitals.installsCount.query → installs per week
    5. vitals.crashRateMetricSet.query → crashes (для алертов)
    6. app.reviews.query → rating delta

Env required:
    GOOGLE_PLAY_SA_JSON   — raw JSON content
    PLAY_PACKAGE_CENTRY   — bundle id, например ru.fortonlab.centry
    PLAY_PACKAGE_DIKTUM   — same для Diktum

Current state: SKELETON.
"""
from __future__ import annotations

import datetime as dt
import os

from .models import Product, StoreSnapshot

_MOCK_INSTALLS: dict[Product, int] = {"centry": 11, "diktum": 9}
_MOCK_PREV: dict[Product, int] = {"centry": 16, "diktum": 15}


def _is_configured() -> bool:
    return all(os.environ.get(k) for k in
                ("GOOGLE_PLAY_SA_JSON", "PLAY_PACKAGE_CENTRY", "PLAY_PACKAGE_DIKTUM"))


def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    if not _is_configured():
        return StoreSnapshot(
            product=product, store="google_play", week_start=week_start,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.6 if product == "centry" else 4.5,
            top_country="RU", top_country_share=0.72,
        )
    raise NotImplementedError(
        "Play secrets configured but real implementation not yet wired."
    )


def fetch_previous(product: Product, week_start: dt.date) -> StoreSnapshot:
    if not _is_configured():
        return StoreSnapshot(
            product=product, store="google_play",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.6 if product == "centry" else 4.5,
            top_country="RU", top_country_share=0.70,
        )
    raise NotImplementedError
