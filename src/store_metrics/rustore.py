"""RuStore Public API — попытка JWE-аутентификации + statistics endpoint.

Pipeline (если API отдаёт downloads):
    1. POST /public/auth/ с JWE-токеном (RSA-OAEP + signed header)
    2. Получаем bearer-token TTL=900сек
    3. GET /public/v1/application/{appId}/statistics?from=X&to=Y
    4. Parse installs/uninstalls/rating

Если RuStore не даёт stats через API (открытый вопрос Q3 из research):
    → возвращаем StoreSnapshot с error="Stats недоступны через API; см. Console"
    → fallback documented в `Brain/projects/forton-lab/decisions.md`

Env required:
    RUSTORE_PRIVATE_KEY   — RSA private key (PEM)
    RUSTORE_KEY_ID        — service token Key ID
    RUSTORE_APP_ID_CENTRY — app id из console.rustore.ru/apps/<id>
    RUSTORE_APP_ID_DIKTUM — same

Current state: SKELETON.
"""
from __future__ import annotations

import datetime as dt
import os

from .models import Product, StoreSnapshot

_MOCK_INSTALLS: dict[Product, int] = {"centry": 4, "diktum": 2}
_MOCK_PREV: dict[Product, int] = {"centry": 3, "diktum": 5}


def _is_configured() -> bool:
    return all(os.environ.get(k) for k in
                ("RUSTORE_PRIVATE_KEY", "RUSTORE_KEY_ID",
                 "RUSTORE_APP_ID_CENTRY", "RUSTORE_APP_ID_DIKTUM"))


def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    if not _is_configured():
        return StoreSnapshot(
            product=product, store="rustore", week_start=week_start,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.8 if product == "centry" else 4.7,
            top_country="RU", top_country_share=0.95,
        )
    raise NotImplementedError(
        "RuStore secrets configured but real implementation not yet wired."
    )


def fetch_previous(product: Product, week_start: dt.date) -> StoreSnapshot:
    if not _is_configured():
        return StoreSnapshot(
            product=product, store="rustore",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.8 if product == "centry" else 4.7,
            top_country="RU", top_country_share=0.94,
        )
    raise NotImplementedError
