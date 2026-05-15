"""Google Play metrics adapter — manual CSV installs + androidpublisher reviews.

Pipeline (real-mode, package envs set):
    1. _read_csv_installs: read `.metrics/gplay_weekly/<YYYY-Www>.csv` —
       Google Play Console "Statistics → Export CSV" download (UTF-16 LE BOM,
       comma-separated, daily rows). Filter rows where Package Name == package
       AND Date is within the target ISO week, sum Daily Device Installs,
       group by Country.
    2. _fetch_reviews (optional): GET androidpublisher.googleapis.com/v3/
       applications/<pkg>/reviews via googleapiclient — returns last 7 days
       reviews. Only attempted when a service-account credential is present;
       failures are swallowed so installs still flow through.
    3. fetch_weekly: composes StoreSnapshot from CSV (installs) + API (rating).

Architectural pivot (2026-05-15):
    Earlier hotfix iterations attempted GCS bucket downloads
    (`gs://pubsite_prod_rev_<developer_id>/stats/installs/...`). GH Secret
    handling silently injected whitespace/zero-width chars that broke bucket
    name validation, and diagnostic step output was masked by GH Actions
    secret-redaction — undebuggable remotely. Canonical solution: **manual
    CSV upload** — same pattern as RuStore. 5 min/week user effort, but
    reliable, predictable. Reviews API stays where it works.

Env required (real-mode):
    GPLAY_PACKAGE_CENTRY      — Play package name for Centry (e.g.
                                  "website.centry.app") — used for CSV row
                                  filtering AND reviews API path.
    GPLAY_PACKAGE_DIKTUM      — same for Diktum.

Optional (enables reviews path):
    GOOGLE_PLAY_SA_JSON       — raw service account JSON content, OR
    GOOGLE_PLAY_SA_JSON_PATH  — filesystem path to SA JSON.

Without package envs → mock data. Without SA envs → installs work, rating=None.
Old env GPLAY_DEVELOPER_ID is no longer used by this module (was only needed
for GCS bucket construction).

References:
    - Phase 5 RESEARCH §«Google Play / androidpublisher v3 reviews»
    - Brain decisions 2026-05-14 «GPlay GCS path blocked → manual CSV».
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

from .models import Product, StoreSnapshot

_PLAY_REVIEWS_SCOPE: Final[str] = "https://www.googleapis.com/auth/androidpublisher"

# Safety cap to prevent runaway pagination loops on androidpublisher reviews.list.
# Last-7-days volume для двух продуктов студии far below this.
_REVIEWS_PAGE_CAP: Final[int] = 200

_MOCK_INSTALLS: dict[Product, int] = {"centry": 11, "diktum": 9}
_MOCK_PREV: dict[Product, int] = {"centry": 16, "diktum": 15}

_REQUIRED_ENVS: Final[tuple[str, ...]] = (
    "GPLAY_PACKAGE_CENTRY",
    "GPLAY_PACKAGE_DIKTUM",
)


# ===================================================================
# Configuration
# ===================================================================

def _is_configured() -> bool:
    """True iff both GPLAY_PACKAGE_* envs are set (non-empty).

    SA credentials are optional — installs read from CSV without auth;
    reviews API path uses SA only when available.
    """
    return all(os.environ.get(k) for k in _REQUIRED_ENVS)


def _has_sa_credentials() -> bool:
    """True iff one of (raw-JSON env, path env) is set for the reviews path."""
    return bool(
        os.environ.get("GOOGLE_PLAY_SA_JSON")
        or os.environ.get("GOOGLE_PLAY_SA_JSON_PATH")
    )


def _package_for(product: Product) -> str:
    """Resolve Play package name per product, stripping whitespace."""
    key = "GPLAY_PACKAGE_CENTRY" if product == "centry" else "GPLAY_PACKAGE_DIKTUM"
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} not set")
    return val


# ===================================================================
# Credentials (only needed for reviews path)
# ===================================================================

def _get_credentials():
    """Build service account credentials with androidpublisher scope.

    Prefer raw env content (``GOOGLE_PLAY_SA_JSON``). If absent, fall back to
    ``GOOGLE_PLAY_SA_JSON_PATH`` (local dev).
    """
    # Lazy import: avoid loading google-auth at module import time in mock mode.
    from google.oauth2 import service_account

    scopes = [_PLAY_REVIEWS_SCOPE]
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
# Manual CSV installs reader
# ===================================================================

def _iso_week_key(week_start: dt.date) -> str:
    """Compute filename stem YYYY-Www for the ISO week containing week_start."""
    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _read_csv_installs(
    repo_root: Path,
    week_start: dt.date,
    package: str,
) -> tuple[int | None, dict[str, int]]:
    """Read `.metrics/gplay_weekly/<YYYY-Www>.{csv,txt}`, sum installs for `package`.

    Google Play Console "Statistics → Export CSV" produces UTF-16 LE BOM
    encoded files with comma-separated daily rows. Header columns include:
    Date, Package Name, Country, Daily Device Installs, Daily Device Uninstalls,
    Daily User Installs, Daily User Uninstalls, Active Device Installs.

    Returns:
        (None, {})   — file missing (soft-fallback flagged by caller).
        (0, {})      — file present but zero install rows for this package
                       in this week.
        (N, {...})   — N installs grouped by Country code.

    Decoding:
        Try UTF-16 first (Play Console default), then utf-8-sig (BOM), then
        plain utf-8 (defensive — некоторые re-exports теряют BOM).

    Filtering:
        - Package Name == package (case-insensitive)
        - week_start <= Date <= week_end (inclusive 7-day window)
        - Daily Device Installs > 0
    """
    iso_key = _iso_week_key(week_start)
    csv_dir = repo_root / ".metrics" / "gplay_weekly"
    candidates = [
        csv_dir / f"{iso_key}.csv",
        csv_dir / f"{iso_key}.txt",
    ]
    file_path = next((p for p in candidates if p.exists()), None)
    if file_path is None:
        return (None, {})

    raw = file_path.read_bytes()
    # Detect UTF-16 by BOM (Play Console default). Otherwise UTF-8 family.
    # Order matters: UTF-8 ASCII content silently "decodes" as UTF-16 into
    # garbage Han characters, so we must check the BOM explicitly first.
    text: str | None = None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = raw.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            text = None
    if text is None:
        for encoding in ("utf-8-sig", "utf-8", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
    if text is None:
        raise RuntimeError(
            f"GPlay CSV {file_path} encoding not recognized "
            "(tried utf-16/utf-8-sig/utf-8)"
        )

    # Strip Unicode BOM if leaked through.
    if text.startswith("﻿"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    week_end = week_start + dt.timedelta(days=6)
    package_norm = package.strip().lower()

    total = 0
    by_country: dict[str, int] = {}
    matched_any_row = False

    for row in reader:
        pkg = (row.get("Package Name") or "").strip().lower()
        if pkg != package_norm:
            # Different package — mark file as having data, skip.
            if pkg:
                matched_any_row = True
            continue
        matched_any_row = True

        date_raw = (row.get("Date") or "").strip()
        try:
            row_date = dt.date.fromisoformat(date_raw)
        except ValueError:
            continue
        if row_date < week_start or row_date > week_end:
            continue

        installs_raw = (row.get("Daily Device Installs") or "0").strip() or "0"
        try:
            installs = int(installs_raw)
        except (ValueError, TypeError):
            continue
        if installs <= 0:
            continue

        country = (row.get("Country") or "").strip().upper()
        total += installs
        if country:
            by_country[country] = by_country.get(country, 0) + installs

    if not matched_any_row:
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
# Repo root resolution
# ===================================================================

def _repo_root() -> Path:
    """Resolve marketing-v3 repo root from this file's location.

    src/store_metrics/play.py → parents[2] == marketing-v3/
    """
    return Path(__file__).resolve().parents[2]


# ===================================================================
# Public API
# ===================================================================

def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Fetch installs (manual CSV) + rating (androidpublisher) for one week.

    week_start = Monday of the target week (ISO).
    Without package envs → mock snapshot (preserves CLI behaviour).

    Composition:
        - installs из manual CSV (юзер положил воскресной задачей).
          Если CSV нет → installs=None, error="GPlay CSV не положен — ...".
        - rating через androidpublisher API (требует SA credentials).
          Если SA нет или API падает → rating=None, installs остаются.
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

    package = _package_for(product)

    # ----- Installs (manual CSV) -----
    installs: int | None = None
    by_country: dict[str, int] = {}
    csv_error: str | None = None
    try:
        installs, by_country = _read_csv_installs(
            _repo_root(), week_start, package,
        )
    except RuntimeError as exc:
        installs = None
        csv_error = f"GPlay CSV error: {exc}"

    csv_missing = installs is None and csv_error is None

    # ----- Reviews (androidpublisher) — optional, only if SA credentials present.
    # Failures are swallowed so installs still flow through.
    rating: float | None = None
    if _has_sa_credentials():
        try:
            credentials = _get_credentials()
            rating, _count = _fetch_reviews(credentials, package)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            sys.stderr.write(
                f"WARN: GPlay reviews fetch failed for {package}: {exc!r}\n"
            )
            rating = None

    top_cc, share = _top_country(by_country)

    if csv_missing:
        error = "GPlay CSV не положен — installs см. Play Console Exports"
    elif csv_error:
        error = csv_error
    else:
        error = None

    return StoreSnapshot(
        product=product,
        store="google_play",
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
            store="google_play",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.6 if product == "centry" else 4.5,
            top_country="RU",
            top_country_share=0.70,
        )
    prev_week_start = week_start - dt.timedelta(days=7)
    return fetch_weekly(product, prev_week_start)
