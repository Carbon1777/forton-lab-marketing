"""Apple App Store metrics adapter — Apple Reporter API + iTunes RSS.

Pipeline (real-mode, all 4 envs set):
    1. _fetch_sales_tsv: POST https://reportingitc-reporter.apple.com/reportservice/sales/v1
       form body `jsonRequest=<urlencoded JSON>` with `accesstoken` field
       INSIDE the JSON body (NOT as Authorization header — Apple Reporter
       Legacy spec). Response body is gzipped TSV (Sales Summary Weekly).
    2. _parse_installs_from_tsv: filter rows where Product Type Identifier == "1F"
       (paid/free download — installs) AND Apple Identifier == app_id; sum Units,
       group by Country Code → top_country.
    3. _fetch_rss_ratings: GET https://itunes.apple.com/<cc>/rss/customerreviews/
       id=<app_id>/sortBy=mostRecent/page=1/json — no auth, last 50 reviews,
       aggregate across RU/US/KZ/BY/UA для weighted avg rating.
    4. fetch_weekly: composes StoreSnapshot из этих трёх кусков.

Env required (real-mode):
    ASC_REPORTER_ACCESS_TOKEN — UUID-style bearer (Reporter Tokens portal,
                                  TTL 180 days)
    ASC_VENDOR_NUMBER         — Apple vendor account number (digits)
    ASC_APP_ID_CENTRY         — numeric Apple App ID for Centry
    ASC_APP_ID_DIKTUM         — numeric Apple App ID for Diktum

Without any of these → fallback to mock data (для local dev / тестов CLI).

References:
    - Brain decisions 2026-05-14 «Apple Reporter Token РАЗБЛОКИРОВАН»
    - Phase 5 RESEARCH §«Per-API Technical Detail → Apple Reporter API»
    - D-5-01: Reporter API path (not Integrations API, which is blocked).
    - D-5-11: graceful degrade on 404 (week report not ready yet).
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import urllib.parse
from typing import Final

from . import _http
from .models import Product, StoreSnapshot

_REPORTER_URL: Final[str] = (
    "https://reportingitc-reporter.apple.com/reportservice/sales/v1"
)
_RSS_URL_TEMPLATE: Final[str] = (
    "https://itunes.apple.com/{cc}/rss/customerreviews/id={app_id}"
    "/sortBy=mostRecent/page=1/json"
)
_RSS_COUNTRIES: Final[tuple[str, ...]] = ("ru", "us", "kz", "by", "ua")

# Product Type Identifier for free/paid app downloads (installs).
# 1F = iPhone/iPad free app, 1 = paid app, 3F = update, IA1 = in-app etc.
# Per spec: installs == rows with PTI "1F" only.
_INSTALL_PTI: Final[str] = "1F"

_MOCK_INSTALLS: dict[Product, int] = {"centry": 23, "diktum": 18}
_MOCK_PREV: dict[Product, int] = {"centry": 19, "diktum": 22}

_REQUIRED_ENVS: Final[tuple[str, ...]] = (
    "ASC_REPORTER_ACCESS_TOKEN",
    "ASC_VENDOR_NUMBER",
    "ASC_APP_ID_CENTRY",
    "ASC_APP_ID_DIKTUM",
)


# ===================================================================
# Configuration
# ===================================================================

def _is_configured() -> bool:
    """True iff ВСЕ 4 env-переменные заданы непустыми строками."""
    return all(os.environ.get(k) for k in _REQUIRED_ENVS)


def _app_id_for(product: Product) -> str:
    """Resolve numeric app id from env per product."""
    key = "ASC_APP_ID_CENTRY" if product == "centry" else "ASC_APP_ID_DIKTUM"
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(f"{key} not set")
    return val


# ===================================================================
# Date helpers
# ===================================================================

def _target_sunday(week_start: dt.date) -> dt.date:
    """Apple Reporter requests weekly reports keyed by the LAST DAY of the week.

    week_start is Monday (ISO week start). Return the Sunday that ends that
    week: week_start + 6 days.

    Note on lag: Apple has a 24-48h delay before a week's report is generated,
    so callers should pass week_start = (today − 7d aligned to Mon) для
    надёжной выборки.
    """
    return week_start + dt.timedelta(days=6)


# ===================================================================
# Apple Reporter API — Sales Summary Weekly
# ===================================================================

def _fetch_sales_tsv(vendor: str, token: str, target_sunday: dt.date) -> bytes:
    """POST Reporter API, return decompressed TSV bytes.

    Returns:
        Decompressed TSV bytes (may be header-only if no sales).
        Empty bytes b'' if API returns 404 (week not yet ready — graceful).

    Raises:
        RuntimeError on 401/403 (token problem).
        requests.HTTPError on 5xx after retries.
    """
    date_str = target_sunday.strftime("%Y%m%d")
    query_input = (
        f"[p=Reports.getReport, {vendor}, Sales, Summary, Weekly, {date_str}]"
    )
    # HOTFIX 2026-05-15 (smoke test run 25890122345 returned 401):
    # Apple Reporter Legacy API expects the access token INSIDE the
    # jsonRequest body as `accesstoken` field, NOT as Authorization HTTP
    # header. Original RESEARCH.md misidentified the auth path as Bearer.
    json_request = {
        "userid": "",
        "password": "",
        "account": vendor.strip(),
        "version": "1.0",
        "mode": "Robot.XML",
        "queryInput": query_input,
        "accesstoken": token.strip(),
    }
    body = "jsonRequest=" + urllib.parse.quote(json.dumps(json_request))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/octet-stream",
    }
    resp = _http.fetch_with_retry(
        url=_REPORTER_URL,
        method="POST",
        headers=headers,
        data=body,
    )
    status = resp.status_code
    if status == 404:
        # Week's report not generated yet (Apple lag) — caller treats as None.
        sys.stderr.write(
            f"INFO: Apple Reporter 404 for week ending {date_str} — "
            "report not ready, returning empty.\n"
        )
        return b""
    if status in (401, 403):
        # HOTFIX: include response excerpt (without secrets — only API's
        # error message) so digest shows actionable error instead of
        # generic "check token".
        snippet = (resp.text or "")[:200].replace("\n", " ")
        raise RuntimeError(
            f"Apple Reporter auth failed (HTTP {status}): {snippet}"
        )
    if status >= 400:
        # 4xx≠401/403/404 — surface as generic failure (no retry by _http).
        resp.raise_for_status()
    try:
        return gzip.decompress(resp.content)
    except (OSError, EOFError) as exc:
        raise RuntimeError(
            f"Apple Reporter response gzip decompress failed: {exc!r}"
        ) from exc


def _parse_installs_from_tsv(
    tsv_bytes: bytes,
    app_id: str,
) -> tuple[int | None, dict[str, int]]:
    """Parse Reporter TSV → (total_installs, by_country dict).

    Returns:
        (None, {}) — if tsv_bytes is empty (report not ready / Apple absent).
        (0, {})    — if file has data but no row matches app_id (= 0 installs).
        (N, {...}) — N installs (PTI=1F + matching Apple Identifier), grouped
                       by Country Code.

    Notes:
        - Robust against header-only TSV (returns (None, {})).
        - csv.DictReader handles BOM-less UTF-8 tab-separated.
    """
    if not tsv_bytes:
        return (None, {})

    text = tsv_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= 1:
        # Only header (or empty) — no data rows at all.
        return (None, {})

    reader = csv.DictReader(io.StringIO(text), delimiter="\t")

    total = 0
    by_country: dict[str, int] = {}
    matched_any_row = False

    for row in reader:
        pti = (row.get("Product Type Identifier") or "").strip()
        apple_id = (row.get("Apple Identifier") or "").strip()
        if apple_id != app_id:
            # Row belongs to a different app — skip but mark file as having data.
            continue
        matched_any_row = True
        if pti != _INSTALL_PTI:
            # Different product type (paid app, update, IAP, etc.) — not install.
            continue
        try:
            units = int((row.get("Units") or "0").strip())
        except ValueError:
            continue
        if units <= 0:
            continue
        country = (row.get("Country Code") or "").strip().upper()
        total += units
        if country:
            by_country[country] = by_country.get(country, 0) + units

    if not matched_any_row:
        # File had rows, но ни одной для этого app_id → legit "0 installs".
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
    """Fetch installs + rating + top country for one week.

    week_start = Monday of the target week (ISO).
    Without all 4 envs → returns mock snapshot (preserves CLI behaviour).
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

    vendor = os.environ["ASC_VENDOR_NUMBER"]
    token = os.environ["ASC_REPORTER_ACCESS_TOKEN"]
    app_id = _app_id_for(product)
    target_sun = _target_sunday(week_start)

    try:
        tsv_bytes = _fetch_sales_tsv(vendor, token, target_sun)
    except RuntimeError as exc:
        return StoreSnapshot(
            product=product,
            store="app_store",
            week_start=week_start,
            installs=None,
            error=f"reporter auth failed: {exc}",
        )

    installs, by_country = _parse_installs_from_tsv(tsv_bytes, app_id)
    top_cc, share = _top_country(by_country)
    rating, _count = _fetch_rss_ratings(app_id)

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
        error=None,
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
