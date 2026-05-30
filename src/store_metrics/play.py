"""Google Play metrics adapter — GCS bucket installs CSV + androidpublisher v3 reviews.

Pipeline (real-mode, all 4 envs set):
    1. _fetch_installs_csv: download stats/installs/installs_<pkg>_<YYYYMM>_country.csv
       from GCS bucket `pubsite_prod_rev_<developer_id>`. Encoded UTF-16 LE BOM.
       Google Play Console stages ONE monthly CSV per package; each row is a
       (day, country) tuple carrying that day's Daily Device Installs.
    2. _parse_installs_csv: decode UTF-16, filter rows by Package Name +
       week_start..week_end inclusive, sum Daily Device Installs, group by Country.
    3. _fetch_reviews: GET androidpublisher.googleapis.com/v3/applications/<pkg>/reviews
       via googleapiclient — returns last 7 days reviews (API limitation; perfect
       for weekly digest). Paginate via tokenPagination.nextPageToken (cap 200).
    4. fetch_weekly: composes StoreSnapshot from these pieces. A reporting week may
       straddle a month boundary → fetch both monthly CSVs and merge.

Architectural note (per RESEARCH §«Google Play / GCS»):
    Google Play Developer **Reporting** API does NOT return installs/uninstalls —
    it only exposes Vitals (ANR, crash, slow startup). Installs come from MONTHLY
    CSV reports staged in the GCS bucket `pubsite_prod_rev_<developer_id>` by
    Google Play Console. The blob name carries a 6-digit YYYYMM (NOT a daily
    YYYYMMDD) — Google never publishes per-day overview blobs. The current month's
    file is updated through the month; a fully-closed reporting week is present by
    the following Monday.

Env required (real-mode):
    GOOGLE_PLAY_SA_JSON       — raw service account JSON content (multi-line
                                  GH Secret), OR
    GOOGLE_PLAY_SA_JSON_PATH  — filesystem path to SA JSON (local dev).
    GPLAY_DEVELOPER_ID        — numeric developer id used to construct GCS
                                  bucket name (e.g. "6224792403622982347").
    GPLAY_PACKAGE_CENTRY      — Play package name for Centry (e.g.
                                  "website.centry.app").
    GPLAY_PACKAGE_DIKTUM      — same for Diktum.

Without any of these → fallback to mock data (for local dev / CLI tests).

References:
    - Phase 5 RESEARCH §«Per-API Technical Detail → Google Play»
    - Phase 5 CONTEXT D-5-11 (graceful degrade when blob not generated yet)
    - Brain decisions 2026-05-14 «Google Play GCS + androidpublisher dual API»
    - Brain decisions 2026-05-30 «GPlay installs bug: monthly _country.csv, not daily»
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
from typing import Final

from .models import Product, StoreSnapshot

_GCS_SCOPE: Final[str] = "https://www.googleapis.com/auth/devstorage.read_only"
_PLAY_REVIEWS_SCOPE: Final[str] = "https://www.googleapis.com/auth/androidpublisher"

# Safety cap to prevent runaway pagination loops on androidpublisher reviews.list.
# Last-7-days volume для двух продуктов студии far below this.
_REVIEWS_PAGE_CAP: Final[int] = 200

_MOCK_INSTALLS: dict[Product, int] = {"centry": 11, "diktum": 9}
_MOCK_PREV: dict[Product, int] = {"centry": 16, "diktum": 15}

_REQUIRED_BASE_ENVS: Final[tuple[str, ...]] = (
    "GPLAY_DEVELOPER_ID",
    "GPLAY_PACKAGE_CENTRY",
    "GPLAY_PACKAGE_DIKTUM",
)

# Emitted as StoreSnapshot.error when no monthly CSV (or no in-week rows) exists.
# MUST contain a substring listed in digest._BLOCKER_PATTERNS so the digest
# renders a clean «—» row instead of a wall of text (см. digest.py).
_NO_DATA_ERROR: Final[str] = (
    "GPlay installs CSV not yet generated for this week — Google публикует "
    "monthly _country.csv с задержкой в несколько дней; данные появятся, когда "
    "дни недели закроются."
)


# ===================================================================
# Configuration
# ===================================================================

def _is_configured() -> bool:
    """True iff one of (raw-JSON env, path env) is set AND all base envs set.

    Either ``GOOGLE_PLAY_SA_JSON`` (raw JSON content, used in CI GH Secret form)
    OR ``GOOGLE_PLAY_SA_JSON_PATH`` (filesystem path, used in local dev) — но
    хотя бы один из двух обязателен.
    """
    sa_set = bool(
        os.environ.get("GOOGLE_PLAY_SA_JSON")
        or os.environ.get("GOOGLE_PLAY_SA_JSON_PATH")
    )
    return sa_set and all(os.environ.get(k) for k in _REQUIRED_BASE_ENVS)


def _package_for(product: Product) -> str:
    """Resolve Play package name per product.

    HOTFIX 2026-05-15: strip env values — GH Secret storage may include
    trailing whitespace that breaks GCS blob path matching.
    """
    key = "GPLAY_PACKAGE_CENTRY" if product == "centry" else "GPLAY_PACKAGE_DIKTUM"
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} not set")
    return val


# ===================================================================
# Credentials
# ===================================================================

def _get_credentials():
    """Build service account credentials with GCS + androidpublisher scopes.

    Prefer raw env content (``GOOGLE_PLAY_SA_JSON``). If absent, fall back to
    ``GOOGLE_PLAY_SA_JSON_PATH`` (local dev).

    Returns:
        google.oauth2.service_account.Credentials with both scopes attached.
    """
    # Lazy import: avoid loading google-auth at module import time in mock mode.
    from google.oauth2 import service_account

    scopes = [_GCS_SCOPE, _PLAY_REVIEWS_SCOPE]
    raw = os.environ.get("GOOGLE_PLAY_SA_JSON")
    if raw:
        sa_info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(
            sa_info, scopes=scopes,
        )
    path = os.environ.get("GOOGLE_PLAY_SA_JSON_PATH")
    if not path:
        raise RuntimeError(
            "Neither GOOGLE_PLAY_SA_JSON nor GOOGLE_PLAY_SA_JSON_PATH is set"
        )
    return service_account.Credentials.from_service_account_file(
        path, scopes=scopes,
    )


# ===================================================================
# Date helpers
# ===================================================================

def _iso_week_range(week_start: dt.date) -> tuple[dt.date, dt.date]:
    """ISO Monday → (Monday, Sunday) inclusive 7-day range."""
    return (week_start, week_start + dt.timedelta(days=6))


def _months_spanned(week_start: dt.date, week_end: dt.date) -> list[str]:
    """YYYYMM strings the [week_start..week_end] window touches.

    A reporting week that straddles a month boundary (e.g. Apr 27 – May 3)
    needs BOTH monthly CSVs. Returns 1 or 2 entries, week_start-month first.
    """
    months = [week_start.strftime("%Y%m")]
    end_month = week_end.strftime("%Y%m")
    if end_month != months[0]:
        months.append(end_month)
    return months


# ===================================================================
# GCS — installs CSV
# ===================================================================

def _sanitize_dev_id(developer_id: str) -> str:
    """Extract digits-only — bulletproof против GH invisible chars."""
    import re as _re
    return _re.sub(r"\D", "", developer_id)


def _sanitize_package(package: str) -> str:
    """Strip whitespace + BOM/NBSP/zero-width chars."""
    import re as _re
    return _re.sub(r"\s+", "", package).replace("﻿", "").replace(
        "​", ""
    ).replace("\xa0", "")


def _gcs_bucket(credentials, developer_id: str):
    """Construct GCS bucket handle for the developer's reports bucket."""
    from google.cloud import storage
    dev_id_clean = _sanitize_dev_id(developer_id)
    if not dev_id_clean:
        raise RuntimeError(
            f"GPLAY_DEVELOPER_ID has no digits after sanitization "
            f"(len_raw={len(developer_id)}, ascii={developer_id.isascii()})"
        )
    bucket_name = f"pubsite_prod_rev_{dev_id_clean}"
    client = storage.Client(credentials=credentials)
    try:
        return client.bucket(bucket_name), bucket_name
    except ValueError as exc:
        raise RuntimeError(
            f"GCS bucket name invalid: {bucket_name!r} — {exc}"
        ) from exc


def _fetch_installs_csv(
    credentials,
    developer_id: str,
    package: str,
    yyyymm: str,
) -> bytes | None:
    """Download the monthly installs country CSV for one package + month.

    Path: `stats/installs/installs_<package>_<YYYYMM>_country.csv`

    Google Play Console stages exactly one such blob per package per month
    (UTF-16 LE BOM), one row per (day, country). Returns:
        Raw bytes when the blob exists.
        ``None`` when the blob does not exist yet — Google has not generated
        this month's file (very early in a month). Graceful, not an error.
    """
    bucket, bucket_name = _gcs_bucket(credentials, developer_id)
    pkg_clean = _sanitize_package(package)
    blob_path = (
        f"stats/installs/installs_{pkg_clean}_{yyyymm}_country.csv"
    )
    blob = bucket.blob(blob_path)
    if not blob.exists():
        sys.stderr.write(
            f"INFO: Play GCS monthly blob missing: gs://{bucket_name}/{blob_path} — "
            "report not generated yet.\n"
        )
        return None
    return blob.download_as_bytes()


def _decode_gplay_csv(csv_bytes: bytes) -> str:
    """Decode UTF-16 LE BOM (or fallback UTF-8). Shared helper."""
    try:
        return csv_bytes.decode("utf-16")
    except UnicodeDecodeError:
        return csv_bytes.decode("utf-8", errors="replace")


def _parse_installs_csv(
    csv_bytes: bytes,
    package: str,
    week_start: dt.date,
    week_end: dt.date,
) -> tuple[int | None, dict[str, int]]:
    """Parse monthly country CSV → (weekly installs, {country: installs}).

    Monthly country columns (UTF-16 LE BOM):
        Date, Package Name, Country, Daily Device Installs,
        Daily Device Uninstalls, Active Device Installs
    One row per (day, country). Keep rows where Package Name == package AND
    Date (ISO YYYY-MM-DD) ∈ [week_start, week_end] inclusive; sum
    `Daily Device Installs`; group positive installs by Country.

    Returns:
        (None, {}) — empty bytes / header-only file (no data rows at all).
        (0, {})    — data rows exist but none match the package.
        (int, by)  — matched: weekly installs sum + per-country breakdown
                     (zero-install rows count toward the sum but not the geo map).
    """
    if not csv_bytes:
        return (None, {})
    text = _decode_gplay_csv(csv_bytes)
    lines = text.splitlines()
    if len(lines) <= 1:
        return (None, {})

    reader = csv.DictReader(io.StringIO(text))
    pkg_clean = _sanitize_package(package)
    total = 0
    by_country: dict[str, int] = {}
    matched = False
    for row in reader:
        row_pkg = _sanitize_package(row.get("Package Name") or "")
        if row_pkg != pkg_clean:
            continue
        matched = True
        date_raw = (row.get("Date") or "").strip()
        try:
            row_date = dt.date.fromisoformat(date_raw)
        except ValueError:
            continue
        if not (week_start <= row_date <= week_end):
            continue
        installs_raw = (row.get("Daily Device Installs") or "0").strip() or "0"
        try:
            installs = int(installs_raw)
        except ValueError:
            continue
        total += installs
        if installs > 0:
            country = (row.get("Country") or "").strip().upper()
            if country:
                by_country[country] = by_country.get(country, 0) + installs
    if not matched:
        return (0, {})
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
# androidpublisher v3 — reviews
# ===================================================================

def _fetch_reviews(
    credentials,
    package: str,
) -> tuple[float | None, int]:
    """Fetch last-7-days reviews via androidpublisher v3, aggregate avg + count.

    Paginates через ``tokenPagination.nextPageToken`` until exhausted or until
    ``_REVIEWS_PAGE_CAP`` reviews are collected (safety against runaway loops).

    Returns:
        (avg_rating, count) where avg is None when count == 0.

    Resilience:
        Лёгкие защиты: missing/non-int starRating → skip row, don't crash.
        Hard errors (HttpError) propagate — :func:`fetch_weekly` decides how
        to surface (e.g., Forbidden → "GCS access denied" style error).
    """
    # Lazy import — keeps cold-start light in mock mode.
    from googleapiclient.discovery import build

    service = build(
        "androidpublisher", "v3", credentials=credentials,
        cache_discovery=False,
    )

    star_ratings: list[int] = []
    next_token: str | None = None

    while len(star_ratings) < _REVIEWS_PAGE_CAP:
        if next_token:
            resp = service.reviews().list(
                packageName=package, token=next_token,
            ).execute()
        else:
            resp = service.reviews().list(packageName=package).execute()

        reviews = resp.get("reviews", []) if isinstance(resp, dict) else []
        for review in reviews:
            if not isinstance(review, dict):
                continue
            comments = review.get("comments")
            if not isinstance(comments, list) or not comments:
                continue
            first = comments[0]
            if not isinstance(first, dict):
                continue
            uc = first.get("userComment")
            if not isinstance(uc, dict):
                continue
            star_raw = uc.get("starRating")
            if star_raw is None:
                continue
            try:
                star = int(star_raw)
            except (TypeError, ValueError):
                continue
            if 1 <= star <= 5:
                star_ratings.append(star)
                if len(star_ratings) >= _REVIEWS_PAGE_CAP:
                    break

        token_pag = resp.get("tokenPagination") if isinstance(resp, dict) else None
        next_token = (
            token_pag.get("nextPageToken")
            if isinstance(token_pag, dict)
            else None
        )
        if not next_token:
            break

    if not star_ratings:
        return (None, 0)
    return (sum(star_ratings) / len(star_ratings), len(star_ratings))


# ===================================================================
# Public API
# ===================================================================

def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Fetch installs + rating + top country for one week (Google Play).

    week_start = Monday of the target week (ISO).
    Without all envs → mock snapshot (preserves CLI behaviour in dev).

    Installs come from the monthly `_country.csv` blob(s) the week touches
    (1, or 2 when the week straddles a month boundary). On GCS Forbidden
    (mis-scoped SA) → StoreSnapshot with installs=None and error string. No
    in-week data → installs=None + blocker error (renders as «—», never raises).
    """
    if not _is_configured():
        return StoreSnapshot(
            product=product,
            store="google_play",
            week_start=week_start,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.6 if product == "centry" else 4.5,
            top_country="RU",
            top_country_share=0.72,
        )

    developer_id = os.environ["GPLAY_DEVELOPER_ID"].strip()
    package = _package_for(product)
    week_start_d, week_end_d = _iso_week_range(week_start)
    months = _months_spanned(week_start_d, week_end_d)

    # Lazy import — only loaded when we hit the real path.
    from google.api_core import exceptions as gcp_exc

    try:
        credentials = _get_credentials()
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return StoreSnapshot(
            product=product,
            store="google_play",
            week_start=week_start,
            installs=None,
            error=f"credentials build failed: {exc}",
        )

    # ----- Installs (monthly country CSV, filtered to the ISO week) -----
    # Read the monthly `_country.csv` for each month the week touches (1, or 2
    # when the week straddles a month boundary), filter rows to the week, sum
    # Daily Device Installs, merge per-country breakdowns.
    total_installs = 0
    any_data_seen = False
    merged_by_country: dict[str, int] = {}

    try:
        for yyyymm in months:
            csv_bytes = _fetch_installs_csv(
                credentials, developer_id, package, yyyymm,
            )
            if csv_bytes is None:
                continue  # month CSV not generated yet — try the other month
            month_total, month_by_cc = _parse_installs_csv(
                csv_bytes, package, week_start_d, week_end_d,
            )
            if month_total is not None:
                any_data_seen = True
                total_installs += month_total
                for cc, n in month_by_cc.items():
                    merged_by_country[cc] = merged_by_country.get(cc, 0) + n
    except gcp_exc.Forbidden as exc:
        return StoreSnapshot(
            product=product,
            store="google_play",
            week_start=week_start,
            installs=None,
            error=f"GCS access denied (check SA permissions): {exc}",
        )

    installs_final: int | None = total_installs if any_data_seen else None
    top_cc, share = _top_country(merged_by_country)

    # ----- Reviews (androidpublisher) -----
    # Reviews are not required for the snapshot to be useful — degrade gracefully
    # if androidpublisher fails (e.g. SA missing reviews permission). Installs
    # still flow through; rating left as None.
    rating: float | None = None
    try:
        rating, _count = _fetch_reviews(credentials, package)
    except Exception as exc:  # noqa: BLE001 — RSS analog, degrade per-call
        sys.stderr.write(
            f"WARN: Play reviews fetch failed for {package}: {exc!r}\n"
        )

    error = None if installs_final is not None else _NO_DATA_ERROR

    return StoreSnapshot(
        product=product,
        store="google_play",
        week_start=week_start,
        installs=installs_final,
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
            store="google_play",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.6 if product == "centry" else 4.5,
            top_country="RU",
            top_country_share=0.70,
        )
    prev_week_start = week_start - dt.timedelta(days=7)
    return fetch_weekly(product, prev_week_start)
