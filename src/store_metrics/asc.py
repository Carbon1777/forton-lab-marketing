"""Apple App Store metrics adapter — Integrations stub + iTunes RSS ratings.

Pipeline (real-mode, app-id envs set):
    1. **Installs path — stubbed.** Apple Integrations API Key generation
       заблокирован Apple cert recovery (Brain decision 2026-05-14, user
       confirmed ongoing 2026-05-15). Modern Sales Reports API требует JWT
       ES256 bearer signed ASC API Key (Key ID + Issuer ID + .p8 private
       key). Когда Apple Support починит cert — добавим 3 GH Secrets:
       ``ASC_KEY_ID``, ``ASC_ISSUER_ID``, ``ASC_PRIVATE_KEY``, развернём
       JWT signing block ниже. Сейчас installs всегда ``None`` с явным
       ``error`` сообщающим про блокировку.
    2. _fetch_rss_ratings: GET https://itunes.apple.com/<cc>/rss/customerreviews/
       id=<app_id>/sortBy=mostRecent/page=1/json — no auth, last 50 reviews per
       country, aggregated across RU/US/KZ/BY/UA for weighted avg rating.
       RSS failure → rating=None (soft, doesn't break digest).
    3. fetch_weekly: composes StoreSnapshot from stub installs + RSS rating.

Architectural pivot (2026-05-15 final):
    Earlier iterations пытались:
      - modern Sales Reports API + Reporter Token UUID → 400 "improperly
        configured bearer token" (Reporter Token предназначен для
        deprecated itc-reporter API).
      - manual CSV upload → отвергнуто юзером ("я не буду ничего загружать
        руками").
    Canonical solution для Apple после блокера cert recovery: **stub**
    installs с явным error-сообщением, ratings продолжают работать через
    auth-free RSS. Когда Apple вернёт доступ к Integrations и юзер
    сгенерит API Key — добавим JWT signing path ниже, installs включатся
    автоматически.

Env required (real-mode):
    ASC_APP_ID_CENTRY  — numeric Apple App ID для Centry (для RSS lookups).
    ASC_APP_ID_DIKTUM  — numeric Apple App ID для Diktum.

Когда Apple Integrations станет доступен — добавятся:
    ASC_KEY_ID         — ASC API Key ID (e.g. "ABC123XYZ").
    ASC_ISSUER_ID      — Issuer UUID из ASC → Users and Access → Integrations.
    ASC_PRIVATE_KEY    — raw .p8 EC P-256 PEM (multi-line GH Secret).

Без app-id envs → mock data (preserves CLI / dev behaviour). Старые envs
ASC_REPORTER_ACCESS_TOKEN / ASC_VENDOR_NUMBER больше не используются — их
можно удалить из GH Secrets когда удобно (модуль их не читает).

References:
    - Phase 5 RESEARCH §«iTunes RSS Reviews» — RSS endpoint unchanged.
    - Brain decisions 2026-05-14 «Apple Reporter API blocked, Integrations
      blocked by cert recovery».
    - Brain decisions 2026-05-15 «full-auto mode, drop manual CSV».
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Final

from . import _http
from .models import Product, StoreSnapshot

_RSS_URL_TEMPLATE: Final[str] = (
    "https://itunes.apple.com/{cc}/rss/customerreviews/id={app_id}"
    "/sortBy=mostRecent/page=1/json"
)
_RSS_COUNTRIES: Final[tuple[str, ...]] = ("ru", "us", "kz", "by", "ua")

_MOCK_INSTALLS: dict[Product, int] = {"centry": 23, "diktum": 18}
_MOCK_PREV: dict[Product, int] = {"centry": 19, "diktum": 22}

# Only app-id envs are needed для RSS path. Когда cert recovery починят
# и появится Integrations API Key — добавим ASC_KEY_ID/ASC_ISSUER_ID/
# ASC_PRIVATE_KEY в _REQUIRED_ENVS (или в отдельный gate для installs).
_REQUIRED_ENVS: Final[tuple[str, ...]] = (
    "ASC_APP_ID_CENTRY",
    "ASC_APP_ID_DIKTUM",
)

# Stable error string — surfaced в StoreSnapshot.error когда installs
# заблокированы Apple cert recovery. Меняется ТОЛЬКО когда cert recovery
# завершится и мы развернём JWT path (см. модуль docstring).
_INSTALLS_BLOCKER_ERROR: Final[str] = (
    "Apple Integrations API не настроен — ждём Apple cert recovery "
    "(Brain decision 2026-05-14). Когда починят: ASC → Users and Access "
    "→ Integrations → Generate API Key, добавь 3 GH Secrets "
    "(ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY) — разблокирую JWT path."
)


# ===================================================================
# Configuration
# ===================================================================

def _is_configured() -> bool:
    """True iff both ASC_APP_ID_* envs are set (non-empty)."""
    return all(os.environ.get(k) for k in _REQUIRED_ENVS)


def _app_id_for(product: Product) -> str:
    """Resolve numeric Apple app id per product, stripping whitespace."""
    key = "ASC_APP_ID_CENTRY" if product == "centry" else "ASC_APP_ID_DIKTUM"
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} not set")
    return val


# ===================================================================
# iTunes RSS — customer reviews (no auth, last 50 per country)
# ===================================================================

def _fetch_rss_ratings(
    app_id: str,
    countries: list[str] | None = None,
) -> tuple[float | None, int]:
    """Aggregate ratings across countries — weighted average.

    Returns:
        (None, 0) если ни одна страна не вернула ratings.
        (avg, count) — weighted avg by total review count.

    Resilience:
        Per-country errors не валят всю функцию — пропускаем, идём дальше.
        Tolerant к двум RSS-формам: entry as list / entry as single dict /
        feed without entry key (empty app like Diktum at launch).
    """
    if countries is None:
        countries = list(_RSS_COUNTRIES)

    total_sum = 0
    total_count = 0
    for cc in countries:
        url = _RSS_URL_TEMPLATE.format(cc=cc, app_id=app_id)
        try:
            resp = _http.fetch_with_retry(url=url, method="GET")
        except Exception as exc:  # noqa: BLE001 — RSS не критичен
            sys.stderr.write(f"WARN: iTunes RSS {cc} request failed: {exc!r}\n")
            continue
        if resp.status_code >= 400:
            sys.stderr.write(
                f"WARN: iTunes RSS {cc} HTTP {resp.status_code} — skipping\n"
            )
            continue
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"WARN: iTunes RSS {cc} non-JSON: {exc!r}\n")
            continue
        feed = payload.get("feed") if isinstance(payload, dict) else None
        if not isinstance(feed, dict):
            continue
        entries = feed.get("entry")
        if entries is None:
            # Empty app — no reviews in this country (legit для нового Diktum).
            continue
        if isinstance(entries, dict):
            # Single review — RSS returns dict, not list. Wrap.
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rating_node = entry.get("im:rating")
            if not isinstance(rating_node, dict):
                continue
            label = rating_node.get("label")
            try:
                r = int(str(label).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= r <= 5:
                total_sum += r
                total_count += 1

    if total_count == 0:
        return (None, 0)
    return (total_sum / total_count, total_count)


# ===================================================================
# Public API
# ===================================================================

def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Fetch installs (stub) + rating (RSS) for one ISO week.

    week_start = Monday of the target week (ISO).
    Без app-id envs → mock snapshot (preserves CLI behaviour).

    Composition:
        - installs = None (всегда) + error = _INSTALLS_BLOCKER_ERROR.
          Apple Integrations API ещё не настроен — installs физически
          не получить автоматически. Когда cert recovery завершится и
          юзер сгенерит API Key — добавим JWT path и снимем stub.
        - rating из iTunes RSS (no auth, no blocking).
          Если RSS падает → rating=None, но installs всё равно None
          с тем же блокер-error (RSS — отдельная axis).
    """
    if not _is_configured():
        return StoreSnapshot(
            product=product,
            store="app_store",
            week_start=week_start,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.7 if product == "centry" else 4.6,
            top_country="RU",
            top_country_share=0.78,
        )

    app_id = _app_id_for(product)

    # ----- Installs (Apple Integrations API — blocked) -----
    # Apple Integrations API Key generation заблокирован Apple cert recovery
    # (Brain decision 2026-05-14). Modern Sales Reports API требует JWT ES256
    # signed bearer из ASC API Key (Key ID + Issuer ID + .p8). Когда Apple
    # Support починит — generate API key + set 3 GH Secrets:
    #   ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY
    # Тогда добавим JWT signing block here. Сейчас — stub возвращает None.
    installs: int | None = None
    by_country: dict[str, int] = {}  # noqa: F841 — placeholder для будущего JWT path
    installs_error = _INSTALLS_BLOCKER_ERROR

    # ----- Ratings (iTunes RSS) -----
    rating: float | None = None
    try:
        rating, _count = _fetch_rss_ratings(app_id)
    except Exception as exc:  # noqa: BLE001 — RSS не критичен для digest
        sys.stderr.write(f"WARN: ASC RSS fetch failed for {app_id}: {exc!r}\n")
        rating = None

    return StoreSnapshot(
        product=product,
        store="app_store",
        week_start=week_start,
        installs=installs,
        uninstalls=None,
        rating=rating,
        rating_count=None,
        top_country=None,
        top_country_share=None,
        error=installs_error,
    )


def fetch_previous(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Same as fetch_weekly but shifted one week back."""
    if not _is_configured():
        return StoreSnapshot(
            product=product,
            store="app_store",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.7 if product == "centry" else 4.5,
            top_country="RU",
            top_country_share=0.75,
        )
    prev_week_start = week_start - dt.timedelta(days=7)
    return fetch_weekly(product, prev_week_start)
