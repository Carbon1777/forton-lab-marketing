"""Google Play metrics adapter — GCS bucket installs CSV + androidpublisher v3 reviews.

Pipeline (real-mode, all 4 envs set):
    1. _fetch_installs_csv: download stats/installs/installs_<pkg>_<YYYYMM>_country.csv
       from GCS bucket `pubsite_prod_rev_<developer_id>`. Encoded UTF-16 LE BOM.
    2. _parse_installs_csv: decode UTF-16, filter rows by Package Name +
       week_start..week_end inclusive, sum Daily Device Installs, group by Country.
    3. _fetch_reviews: GET androidpublisher.googleapis.com/v3/applications/<pkg>/reviews
       via googleapiclient — returns last 7 days reviews (API limitation; perfect
       for weekly digest). Paginate via tokenPagination.nextPageToken (cap 200).
    4. fetch_weekly: composes StoreSnapshot from these pieces.

Architectural note (per RESEARCH §«Google Play / GCS»):
    Google Play Developer **Reporting** API does NOT return installs/uninstalls —
    it only exposes Vitals (ANR, crash, slow startup). Installs come from monthly
    CSV reports staged in the GCS bucket `pubsite_prod_rev_<developer_id>` by
    Google Play Console every night. That's the only first-party path.

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


def _target_dates(week_start: dt.date, week_end: dt.date) -> list[dt.date]:
    """Return list of `dt.date` from week_start to week_end inclusive (7 dates).

    Google Play GCS generates daily overview CSVs:
        installs_<pkg>_YYYYMMDD_overview.csv
    На каждый день с ~24h lag. Для weekly cadence читаем 7 daily файлов,
    суммируем installs.
    """
    n_days = (week_end - week_start).days + 1
    return [week_start + dt.timedelta(days=i) for i in range(n_days)]


def _last_closed_month_yyyymm(week_start: dt.date) -> str:
    """Return YYYYMM string of the previous fully-closed month.

    Используется для fetching country breakdown как proxy: monthly
    `_country.csv` доступен только в начале следующего месяца, daily
    overview не имеет country column. Берём last закрытый month как
    приблизительный country split.
    """
    first_day_of_month = week_start.replace(day=1)
    last_day_prev_month = first_day_of_month - dt.timedelta(days=1)
    return last_day_prev_month.strftime("%Y%m")


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


def _fetch_daily_overview(
    credentials,
    developer_id: str,
    package: str,
    target_date: dt.date,
) -> bytes | None:
    """Download daily overview installs CSV for a specific date.

    Path: `stats/installs/installs_<package>_<YYYYMMDD>_overview.csv`

    Returns:
        Raw bytes (UTF-16 LE BOM-encoded CSV) when blob exists.
        ``None`` when the blob does not exist — Google has ~24h lag, plus
        future dates won't have data yet.
    """
    bucket, bucket_name = _gcs_bucket(credentials, developer_id)
    pkg_clean = _sanitize_package(package)
    yyyymmdd = target_date.strftime("%Y%m%d")
    blob_path = (
        f"stats/installs/installs_{pkg_clean}_{yyyymmdd}_overview.csv"
    )
    blob = bucket.blob(blob_path)
    if not blob.exists():
        sys.stderr.write(
            f"INFO: Play GCS daily blob missing: gs://{bucket_name}/{blob_path} — "
            "report not generated yet, skipping day.\n"
        )
        return None
    return blob.download_as_bytes()


def _fetch_monthly_country(
    credentials,
    developer_id: str,
    package: str,
    yyyymm: str,
) -> bytes | None:
    """Download monthly country breakdown CSV (proxy для top_country).

    Path: `stats/installs/installs_<package>_<YYYYMM>_country.csv`

    Returns:
        Raw bytes or None if blob missing. Used as country proxy when
        daily overviews don't carry country breakdown.
    """
    bucket, bucket_name = _gcs_bucket(credentials, developer_id)
    pkg_clean = _sanitize_package(package)
    blob_path = (
        f"stats/installs/installs_{pkg_clean}_{yyyymm}_country.csv"
    )
    blob = bucket.blob(blob_path)
    if not blob.exists():
        sys.stderr.write(
            f"INFO: Play GCS monthly country blob missing: "
            f"gs://{bucket_name}/{blob_path} — country proxy unavailable.\n"
        )
        return None
    return blob.download_as_bytes()


def _decode_gplay_csv(csv_bytes: bytes) -> str:
    """Decode UTF-16 LE BOM (or fallback UTF-8). Shared helper."""
    try:
        return csv_bytes.decode("utf-16")
    except UnicodeDecodeError:
        return csv_bytes.decode("utf-8", errors="replace")


def _parse_installs_daily(
    csv_bytes: bytes,
    package: str,
) -> int | None:
    """Parse daily overview CSV → total installs for one day.

    Daily overview columns (NO country):
        Date, Package Name, Daily Device Installs, Daily Device Uninstalls,
        Daily User Installs, Daily User Uninstalls,
        Active Device Installs, Install events, Update events, Uninstall events

    Typically 1 row per package per day. Returns sum of `Daily Device Installs`.

    Returns:
        None  — empty/header-only file
        int   — installs sum (may be 0)
    """
    if not csv_bytes:
        return None
    text = _decode_gplay_csv(csv_bytes)
    lines = text.splitlines()
    if len(lines) <= 1:
        return None

    reader = csv.DictReader(io.StringIO(text))
    pkg_clean = _sanitize_package(package)
    total = 0
    matched = False
    for row in reader:
        row_pkg = _sanitize_package(row.get("Package Name") or "")
        if row_pkg != pkg_clean:
            continue
        matched = True
        installs_raw = (row.get("Daily Device Installs") or "0").strip() or "0"
        try:
            installs = int(installs_raw)
        except ValueError:
            continue
        if installs > 0:
            total += installs
    if not matched:
        return 0
    return total


def _parse_country_proxy(
    csv_bytes: bytes,
    package: str,
) -> dict[str, int]:
    """Parse monthly country breakdown CSV → {country: installs_count} aggregate.

    Used as proxy for `top_country` since daily overview CSVs lack country
    information. Aggregates across the whole month — приближение, но lучше
    чем ничего.
    """
    if not csv_bytes:
        return {}
    text = _decode_gplay_csv(csv_bytes)
    lines = text.splitlines()
    if len(lines) <= 1:
        return {}

    reader = csv.DictReader(io.StringIO(text))
    pkg_clean = _sanitize_package(package)
    by_country: dict[str, int] = {}
    for row in reader:
        row_pkg = _sanitize_package(row.get("Package Name") or "")
        if row_pkg != pkg_clean:
            continue
        installs_raw = (row.get("Daily Device Installs") or "0").strip() or "0"
        try:
            installs = int(installs_raw)
        except ValueError:
            continue
        if installs <= 0:
            continue
        country = (row.get("Country") or "").strip().upper()
        if country:
            by_country[country] = by_country.get(country, 0) + installs
    return by_country


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

    On GCS Forbidden (mis-scoped SA) → StoreSnapshot with installs=None and
    error string. On generic exception — propagate to caller (cli wraps).
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
    dates = _target_dates(week_start_d, week_end_d)

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

    # ----- Installs (GCS daily overview aggregation) -----
    # Read 7 daily overview CSVs per week. Each daily CSV содержит installs
    # на один день. Aggregate → weekly total. Daily generation: ~24h lag,
    # so this week's data появляется день-в-день.
    total_installs = 0
    any_data_seen = False

    try:
        for target_date in dates:
            csv_bytes = _fetch_daily_overview(
                credentials, developer_id, package, target_date,
            )
            if csv_bytes is None:
                continue  # Day not generated yet — try other days
            day_total = _parse_installs_daily(csv_bytes, package)
            if day_total is not None:
                any_data_seen = True
                total_installs += day_total

        # ----- Top country proxy (monthly country CSV from prev closed month) -----
        # Daily overview CSV doesn't carry country breakdown. Fetch monthly
        # country CSV from previous closed month как приблизительный split.
        merged_by_country: dict[str, int] = {}
        try:
            country_yyyymm = _last_closed_month_yyyymm(week_start_d)
            country_bytes = _fetch_monthly_country(
                credentials, developer_id, package, country_yyyymm,
            )
            if country_bytes:
                merged_by_country = _parse_country_proxy(country_bytes, package)
        except gcp_exc.NotFound:
            pass  # No prev month data — top_country остаётся None

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

    error = None
    if installs_final is None:
        error = (
            "GPlay daily CSVs not yet available for this week — Google has "
            "~24h lag, data appears day-after-day. Будет реальное число "
            "когда дни закроются (типично 2-3 дня после конца недели)."
        )

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
