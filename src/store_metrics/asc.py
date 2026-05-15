"""Apple App Store metrics adapter — manual CSV installs + iTunes RSS ratings.

Pipeline (real-mode, app-id envs set):
    1. _read_csv_installs: read `.metrics/asc_weekly/<YYYY-Www>.csv` (or .tsv/.txt)
       — Apple "Sales and Trends → Reports → Weekly Summary" download
       (28-col tab-separated TSV by default; we accept both .csv and .tsv).
       Filter rows where Apple Identifier == app_id AND Product Type Identifier == "1F"
       (free first-download = install), sum Units, group by Country Code.
       File missing → installs=None + soft-fallback error string.
    2. _fetch_rss_ratings: GET https://itunes.apple.com/<cc>/rss/customerreviews/
       id=<app_id>/sortBy=mostRecent/page=1/json — no auth, last 50 reviews per
       country, aggregated across RU/US/KZ/BY/UA for weighted avg rating.
       RSS failure → rating=None (soft, doesn't break digest).
    3. fetch_weekly: composes StoreSnapshot from CSV (installs) + RSS (rating).

Architectural pivot (2026-05-15):
    Earlier hotfix iterations attempted modern App Store Connect Sales Reports
    API (Bearer token + GET /v1/salesReports). User's Reporter Token is a UUID
    for the **deprecated legacy itc-reporter API**, which Apple's modern endpoint
    rejects as "improperly configured bearer token". The Integrations path
    (JWT from ASC API Key) is blocked by user's cert recovery. Canonical
    solution: **manual CSV upload** — same pattern as RuStore (which has no
    statistics API at all). 5 min/week user effort, but reliable, predictable,
    doesn't depend on Apple flakiness.

Env required (real-mode):
    ASC_APP_ID_CENTRY  — numeric Apple App ID for Centry (for RSS lookups
                          AND for filtering rows in the CSV).
    ASC_APP_ID_DIKTUM  — numeric Apple App ID for Diktum.

Without these → mock data (preserves CLI / dev behaviour). Old envs
ASC_REPORTER_ACCESS_TOKEN / ASC_VENDOR_NUMBER are no longer used by this
module (kept in GH Secrets for now, can be deleted post-merge).

References:
    - Phase 5 RESEARCH §«iTunes RSS Reviews» — RSS endpoint unchanged.
    - Brain decisions 2026-05-14 «Apple Reporter API blocked → manual CSV».
    - Apple Sales Reports CSV/TSV column reference (28 columns):
      https://developer.apple.com/help/app-store-connect/reference/sales-and-trends-reports/
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
from pathlib import Path
from typing import Final

from . import _http
from .models import Product, StoreSnapshot

_RSS_URL_TEMPLATE: Final[str] = (
    "https://itunes.apple.com/{cc}/rss/customerreviews/id={app_id}"
    "/sortBy=mostRecent/page=1/json"
)
_RSS_COUNTRIES: Final[tuple[str, ...]] = ("ru", "us", "kz", "by", "ua")

# Product Type Identifier for free/paid app downloads (installs).
# 1F = iPhone/iPad free app first download = install.
# 1 = paid app, 3F = update, IA1 = in-app etc. — excluded.
_INSTALL_PTI: Final[str] = "1F"

_MOCK_INSTALLS: dict[Product, int] = {"centry": 23, "diktum": 18}
_MOCK_PREV: dict[Product, int] = {"centry": 19, "diktum": 22}

# Only app-id envs are needed now — no bearer token, no vendor number.
_REQUIRED_ENVS: Final[tuple[str, ...]] = (
    "ASC_APP_ID_CENTRY",
    "ASC_APP_ID_DIKTUM",
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
# Manual CSV installs reader
# ===================================================================

def _iso_week_key(week_start: dt.date) -> str:
    """Compute filename stem YYYY-Www for the ISO week containing week_start."""
    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _read_csv_installs(
    repo_root: Path,
    week_start: dt.date,
    app_id: str,
) -> tuple[int | None, dict[str, int]]:
    """Read `.metrics/asc_weekly/<YYYY-Www>.{csv,tsv,txt}`, sum installs for `app_id`.

    Apple's "Sales and Trends → Reports → Weekly Summary" download is a
    **tab-separated** file. We default to TSV parsing but accept several
    extensions (.csv .tsv .txt) for user convenience.

    Returns:
        (None, {})   — file missing (soft-fallback flagged by caller).
        (0, {})      — file present, headers OK, but zero matching rows for
                       this app_id (file exists but the app had no installs
                       OR Apple hasn't generated rows for this app yet).
        (N, {...})   — N installs grouped by Country Code (PTI == "1F" and
                       Apple Identifier == app_id).

    Decoding:
        Apple Sales Reports are usually UTF-8. We try UTF-8, then UTF-8-sig,
        then UTF-16 as defensive fallbacks (some older exports used UTF-16).

    Filtering:
        - Apple Identifier == app_id  (numeric Apple App ID)
        - Product Type Identifier == "1F"  (free app first download = install)
        - Units > 0
    """
    iso_key = _iso_week_key(week_start)
    csv_dir = repo_root / ".metrics" / "asc_weekly"
    candidates = [
        csv_dir / f"{iso_key}.csv",
        csv_dir / f"{iso_key}.tsv",
        csv_dir / f"{iso_key}.txt",
    ]
    file_path = next((p for p in candidates if p.exists()), None)
    if file_path is None:
        return (None, {})

    raw = file_path.read_bytes()
    # Detect UTF-16 by BOM first (defensive — some older Apple exports used
    # UTF-16). UTF-8 ASCII content silently "decodes" as UTF-16 into garbage,
    # so we must check the BOM explicitly before trying that codec.
    text: str | None = None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = raw.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            text = None
    if text is None:
        for encoding in ("utf-8", "utf-8-sig", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
    if text is None:
        raise RuntimeError(
            f"ASC CSV {file_path} encoding not recognized "
            "(tried utf-8/utf-8-sig/utf-16)"
        )

    # Strip Unicode BOM if any leaked through utf-8 decode.
    if text.startswith("﻿"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text), delimiter="\t")

    total = 0
    by_country: dict[str, int] = {}
    matched_any_row = False

    for row in reader:
        # Tolerant header lookup — Apple TSV exact column names.
        pti = (row.get("Product Type Identifier") or "").strip()
        apple_id = (row.get("Apple Identifier") or "").strip()
        units_raw = (row.get("Units") or "0").strip()
        country = (row.get("Country Code") or "").strip().upper()

        if apple_id != app_id:
            # Row for a different app — mark file as having data, skip.
            if apple_id:
                matched_any_row = True
            continue
        matched_any_row = True

        if pti != _INSTALL_PTI:
            # Different product type (paid app, update, IAP) — not an install.
            continue
        try:
            units = int(units_raw or "0")
        except (ValueError, TypeError):
            continue
        if units <= 0:
            continue
        total += units
        if country:
            by_country[country] = by_country.get(country, 0) + units

    if not matched_any_row:
        # File had no data for this or any app — treat as "no data" rather
        # than legit zero. Same semantics as missing file from digest POV.
        return (None, {})
    return (total, by_country)


def _top_country(
    by_country: dict[str, int],
) -> tuple[str | None, float | None]:
    """Pick highest-install country и его долю (0.0..1.0)."""
    if not by_country:
        return (None, None)
    total = sum(by_country.values())
    if total <= 0:
        return (None, None)
    top_cc, top_n = max(by_country.items(), key=lambda kv: kv[1])
    return (top_cc, top_n / total)


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
# Repo root resolution
# ===================================================================

def _repo_root() -> Path:
    """Resolve marketing-v3 repo root from this file's location.

    src/store_metrics/asc.py → parents[2] == marketing-v3/
    """
    return Path(__file__).resolve().parents[2]


# ===================================================================
# Public API
# ===================================================================

def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Fetch installs (manual CSV) + rating (RSS) for one ISO week.

    week_start = Monday of the target week (ISO).
    Without all app-id envs → mock snapshot (preserves CLI behaviour).

    Composition:
        - installs из manual CSV (юзер положил воскресной задачей).
          Если CSV нет → installs=None, error="ASC CSV не положен — installs см. ASC UI".
        - rating из iTunes RSS (no auth, no blocking).
          Если RSS падает → rating=None, но installs остаются.
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

    # ----- Installs (manual CSV) -----
    installs: int | None = None
    by_country: dict[str, int] = {}
    csv_error: str | None = None
    try:
        installs, by_country = _read_csv_installs(
            _repo_root(), week_start, app_id,
        )
    except RuntimeError as exc:
        installs = None
        csv_error = f"ASC CSV error: {exc}"

    csv_missing = installs is None and csv_error is None

    # ----- Ratings (iTunes RSS) -----
    rating: float | None = None
    try:
        rating, _count = _fetch_rss_ratings(app_id)
    except Exception as exc:  # noqa: BLE001 — RSS не критичен для digest
        sys.stderr.write(f"WARN: ASC RSS fetch failed for {app_id}: {exc!r}\n")
        rating = None

    top_cc, share = _top_country(by_country)

    if csv_missing:
        error = "ASC CSV не положен — installs см. ASC UI (Sales and Trends)"
    elif csv_error:
        error = csv_error
    else:
        error = None

    return StoreSnapshot(
        product=product,
        store="app_store",
        week_start=week_start,
        installs=installs,
        uninstalls=None,
        rating=rating,
        rating_count=None,
        top_country=top_cc,
        top_country_share=share,
        error=error,
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
