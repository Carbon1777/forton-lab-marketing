"""RuStore metrics adapter — JWS RSA-SHA512 auth + reviews API + manual CSV installs.

Pipeline (real-mode, all envs set):
    1. _authenticate: POST /public/auth/ с JSON {keyId, timestamp, signature}.
       signature = base64(RSA-SHA512-PKCS1v15 over (keyId+timestamp)).
       Response body.jwe = bearer token (TTL 900 sec).
    2. _cached_token: module-level cache, перевыпускаем токен при истечении 870с
       safety margin (single workflow run = ~30 сек, одной авторизации хватит).
    3. _fetch_reviews: GET /public/v1/application/<packageName>/comment
       Header: Public-Token: <token>. Pagination via page/size + body.last.
       Filter commentStatus == "PUBLISHED", aggregate appRating 1-5.
    4. _read_csv_installs: читает manual CSV из
       .metrics/rustore_weekly/<YYYY-WW>.csv. Tolerant к UTF-8 BOM + Cyrillic
       заголовкам (Приложение / Установки / Страна). Filter по packageName.
    5. fetch_weekly: композирует StoreSnapshot из CSV (installs) + API (rating).

Architectural notes:
    - RuStore Public API НЕ отдаёт installs (Brain decision 2026-05-14,
      verified UI Console — 9 методов "Общие методы", нет getAppsList и нет
      statistics). Installs → manual CSV workflow (D-5-04).
    - Auth — это JWS (не JWE), хоть response field и называется "jwe"
      (RESEARCH §5 «RuStore Authorization»). Implementation использует
      cryptography PKCS1v15 + SHA512 напрямую, без PyJWT.
    - Reviews paginate через page=0,1,2,...&size=100 + body.last флаг —
      не nextPageToken (отличие от GPlay).

Env required (real-mode):
    RUSTORE_PRIVATE_KEY        — raw PEM content (multi-line GH Secret), OR
    RUSTORE_PRIVATE_KEY_PATH   — filesystem path to PEM (local dev).
    RUSTORE_KEY_ID             — service token Key ID (e.g. "2351028465").
    RUSTORE_COMPANY_ID         — RuStore company ID (metadata; не используется
                                  в auth, но требуется в env для sanity).
    RUSTORE_PACKAGE_CENTRY     — Centry packageName (same as Play).
    RUSTORE_PACKAGE_DIKTUM     — Diktum packageName (same as Play).

Without these → mock data (preserves CLI / dev behaviour).

References:
    - Phase 5 RESEARCH §5 «RuStore Authorization» / §6 «Reviews» / §7 «CSV»
    - Phase 5 CONTEXT D-5-04 (CSV manual workflow + soft-fallback)
    - Brain decisions 2026-05-14 «RuStore API статистики НЕТ → Q3 закрыт»
"""
from __future__ import annotations

import base64
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

# ===================================================================
# Endpoints
# ===================================================================

_AUTH_URL: Final[str] = "https://public-api.rustore.ru/public/auth/"
_COMMENT_URL_TEMPLATE: Final[str] = (
    "https://public-api.rustore.ru/public/v1/application/{package}/comment"
)

# ===================================================================
# Pagination + caching limits
# ===================================================================

# Safety cap на pagination — для двух продуктов студии за всё время
# никогда не приближаемся к этому числу (totalElements ~10-50 в год).
_REVIEWS_PAGE_CAP: Final[int] = 5
_REVIEWS_PAGE_SIZE: Final[int] = 100

# Token TTL = 900s. Берём 870 → 30s safety margin перед истечением.
_TOKEN_TTL_SAFE_SECONDS: Final[int] = 870

# Russian timezone — RuStore docs показывают timestamp с +03:00 offset.
_RUSTORE_TZ: Final[dt.timezone] = dt.timezone(dt.timedelta(hours=3))

# ===================================================================
# Module-level token cache (in-process, single workflow run)
# ===================================================================

_TOKEN_CACHE: dict[str, object] = {"token": None, "expires_at": None}

# ===================================================================
# Mock fallbacks (CLI / local dev)
# ===================================================================

_MOCK_INSTALLS: dict[Product, int] = {"centry": 4, "diktum": 2}
_MOCK_PREV: dict[Product, int] = {"centry": 3, "diktum": 5}

_REQUIRED_BASE_ENVS: Final[tuple[str, ...]] = (
    "RUSTORE_KEY_ID",
    "RUSTORE_COMPANY_ID",
    "RUSTORE_PACKAGE_CENTRY",
    "RUSTORE_PACKAGE_DIKTUM",
)

# ===================================================================
# CSV header recognition (tolerant: EN + RU; case-insensitive)
# ===================================================================

_HEADER_APP: Final[frozenset[str]] = frozenset({
    "app", "package", "package name", "приложение", "application",
})
_HEADER_INSTALLS: Final[frozenset[str]] = frozenset({
    "installs", "installs_count", "downloads", "установки", "загрузки",
    "скачивания",
})
_HEADER_COUNTRY: Final[frozenset[str]] = frozenset({
    "country", "country code", "iso country", "страна",
})


# ===================================================================
# Configuration
# ===================================================================

def _is_configured() -> bool:
    """True iff one of (raw-PEM env, path env) is set AND all base envs set.

    Либо ``RUSTORE_PRIVATE_KEY`` (raw PEM, GH Secret form), либо
    ``RUSTORE_PRIVATE_KEY_PATH`` (path, local dev) — хотя бы один.
    Плюс KEY_ID + COMPANY_ID + 2 package names.
    """
    pk_set = bool(
        os.environ.get("RUSTORE_PRIVATE_KEY")
        or os.environ.get("RUSTORE_PRIVATE_KEY_PATH")
    )
    return pk_set and all(os.environ.get(k) for k in _REQUIRED_BASE_ENVS)


def _package_for(product: Product) -> str:
    """Resolve RuStore packageName per product (same identifiers as GPlay)."""
    key = "RUSTORE_PACKAGE_CENTRY" if product == "centry" else "RUSTORE_PACKAGE_DIKTUM"
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(f"{key} not set")
    return val


# ===================================================================
# Private key loading
# ===================================================================

def _load_private_key():
    """Load RSA private key from env (raw PEM) or filesystem path.

    Returns:
        cryptography.hazmat.primitives.asymmetric.rsa.RSAPrivateKey

    Raises:
        RuntimeError if neither env is set OR PEM cannot be parsed.
    """
    # Lazy import — heavy crypto only when configured.
    from cryptography.hazmat.primitives import serialization

    raw = os.environ.get("RUSTORE_PRIVATE_KEY")
    if raw:
        pem_bytes = raw.encode("utf-8")
    else:
        path = os.environ.get("RUSTORE_PRIVATE_KEY_PATH")
        if not path:
            raise RuntimeError(
                "Neither RUSTORE_PRIVATE_KEY nor RUSTORE_PRIVATE_KEY_PATH is set"
            )
        try:
            pem_bytes = Path(path).read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"Failed to read RUSTORE_PRIVATE_KEY_PATH={path}: {exc}"
            ) from exc

    try:
        return serialization.load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Failed to parse RuStore private key as PEM: {exc}"
        ) from exc


# ===================================================================
# JWS signing
# ===================================================================

def _sign_jws(key_id: str, timestamp: str, private_key) -> str:
    """Sign concat(key_id + timestamp) with RSA-SHA512-PKCS1v15.

    Returns:
        Base64-encoded signature (ASCII string suitable for JSON body).

    Note:
        RESEARCH §5 verified — signature is **base64**, not hex, and not
        url-safe base64. Standard base64 with `=` padding.
        Message bytes: utf-8 encoding of `key_id + timestamp` concatenated
        without separator.
    """
    # Lazy import — paired with _load_private_key.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    message = (key_id + timestamp).encode("utf-8")
    signature_bytes = private_key.sign(
        message,
        padding.PKCS1v15(),
        hashes.SHA512(),
    )
    return base64.b64encode(signature_bytes).decode("ascii")


# ===================================================================
# Authentication — POST /public/auth/
# ===================================================================

def _authenticate() -> str:
    """Perform one-shot auth, return bearer token string.

    Raises:
        RuntimeError on response code != "OK", missing jwe field, или HTTP 4xx.
        requests.HTTPError on 5xx after retries.
    """
    private_key = _load_private_key()
    key_id = os.environ["RUSTORE_KEY_ID"]
    timestamp = dt.datetime.now(tz=_RUSTORE_TZ).isoformat(timespec="milliseconds")
    signature = _sign_jws(key_id, timestamp, private_key)

    body = {
        "keyId": key_id,
        "timestamp": timestamp,
        "signature": signature,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = _http.fetch_with_retry(
        url=_AUTH_URL,
        method="POST",
        headers=headers,
        json_body=body,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"RuStore auth HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"RuStore auth non-JSON response: {exc!r}") from exc

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != "OK":
        raise RuntimeError(f"RuStore auth failed: {payload}")

    inner = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        raise RuntimeError(f"RuStore auth body missing/invalid: {payload}")

    # Field is named "jwe" в API despite being a bearer / JWS token — keep
    # exactly как docs показывают (см. RESEARCH §5 response shape).
    token = inner.get("jwe")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"RuStore auth body.jwe missing: {inner}")
    return token


def _cached_token() -> str:
    """Return cached bearer token, refetching if absent or near-expired.

    Cache stored in module-level dict — fine for single workflow invocation.
    Refetch trigger: token absent OR expires within next 30 seconds.
    """
    now = dt.datetime.now(tz=dt.timezone.utc)
    token = _TOKEN_CACHE.get("token")
    expires_at = _TOKEN_CACHE.get("expires_at")
    if (
        isinstance(token, str)
        and token
        and isinstance(expires_at, dt.datetime)
        and expires_at > now + dt.timedelta(seconds=30)
    ):
        return token
    fresh = _authenticate()
    _TOKEN_CACHE["token"] = fresh
    _TOKEN_CACHE["expires_at"] = now + dt.timedelta(seconds=_TOKEN_TTL_SAFE_SECONDS)
    return fresh


def _reset_token_cache() -> None:
    """Test helper — drop cached token (called by tests, not production code)."""
    _TOKEN_CACHE["token"] = None
    _TOKEN_CACHE["expires_at"] = None


# ===================================================================
# Reviews — GET /public/v1/application/<package>/comment
# ===================================================================

def _fetch_reviews(bearer: str, package: str) -> tuple[float | None, int]:
    """Aggregate avg rating + count over published reviews.

    Pagination:
        page=0,1,2,... через query param, max page = _REVIEWS_PAGE_CAP - 1.
        Останавливается когда body.last == True или content пустой.

    Filtering:
        Считаем только commentStatus == "PUBLISHED" с appRating in 1..5.
        Скрытые / отклонённые отзывы из суммы выбрасываем.

    Returns:
        (avg_rating, count). (None, 0) если ни одного отзыва не нашли.

    Resilience:
        Per-page HTTP errors → останавливаем pagination, возвращаем то что
        уже накопили. Если до первой страницы не дошли — пропускаем
        исключение наверх (caller wraps в error string).
    """
    url = _COMMENT_URL_TEMPLATE.format(package=package)
    headers = {
        "Public-Token": bearer,
        "Content-Type": "application/json",
    }

    star_ratings: list[int] = []
    for page in range(_REVIEWS_PAGE_CAP):
        params = {"page": page, "size": _REVIEWS_PAGE_SIZE}
        resp = _http.fetch_with_retry(
            url=url,
            method="GET",
            headers=headers,
            params=params,
        )
        if resp.status_code >= 400:
            sys.stderr.write(
                f"WARN: RuStore reviews HTTP {resp.status_code} on page={page} "
                f"package={package} — stopping pagination\n"
            )
            break
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"WARN: RuStore reviews non-JSON page={page}: {exc!r}\n"
            )
            break
        if not isinstance(payload, dict):
            break
        if payload.get("code") != "OK":
            sys.stderr.write(
                f"WARN: RuStore reviews code != OK on page={page}: {payload}\n"
            )
            break
        body = payload.get("body")
        if not isinstance(body, dict):
            break
        content = body.get("content")
        if not isinstance(content, list) or not content:
            # Empty page — done.
            break
        for review in content:
            if not isinstance(review, dict):
                continue
            status = review.get("commentStatus")
            if status != "PUBLISHED":
                continue
            star_raw = review.get("appRating")
            if star_raw is None:
                continue
            try:
                star = int(star_raw)
            except (TypeError, ValueError):
                continue
            if 1 <= star <= 5:
                star_ratings.append(star)
        # Stop if API indicates last page.
        if body.get("last") is True:
            break

    if not star_ratings:
        return (None, 0)
    return (sum(star_ratings) / len(star_ratings), len(star_ratings))


# ===================================================================
# Manual CSV installs reader
# ===================================================================

def _iso_week_key(week_start: dt.date) -> str:
    """Compute filename stem YYYY-Www for the ISO week containing week_start."""
    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _resolve_header_map(
    fieldnames: list[str] | None,
) -> dict[str, str] | None:
    """Match CSV headers (case-insensitive, RU+EN tolerant) to canonical names.

    Returns:
        Dict с ключами "app", "installs", optional "country" → original header.
        ``None`` if required columns (app, installs) missing.
    """
    if not fieldnames:
        return None
    mapping: dict[str, str] = {}
    for h in fieldnames:
        if not isinstance(h, str):
            continue
        norm = h.strip().lower()
        if norm in _HEADER_APP and "app" not in mapping:
            mapping["app"] = h
        elif norm in _HEADER_INSTALLS and "installs" not in mapping:
            mapping["installs"] = h
        elif norm in _HEADER_COUNTRY and "country" not in mapping:
            mapping["country"] = h
    if "app" not in mapping or "installs" not in mapping:
        return None
    return mapping


def _read_csv_installs(
    repo_root: Path,
    week_start: dt.date,
    package: str,
) -> tuple[int | None, dict[str, int]]:
    """Read manual RuStore CSV for the ISO week, sum installs for `package`.

    File: ``<repo_root>/.metrics/rustore_weekly/<YYYY-Www>.csv``.

    Returns:
        (None, {})   — file missing (soft-fallback flagged by caller).
        (0, {})      — file present, headers OK, но zero matching rows.
        (N, {...})   — N installs grouped by country (если column есть).

    Decoding:
        Try UTF-8 first; on UnicodeDecodeError fall back to utf-8-sig (BOM).
        utf-8-sig accepts both BOM-prefixed and plain UTF-8 input, so это
        безопасный fallback.

    Filtering:
        Сравниваем app column case-insensitive с package name. Тривиально
        equality сначала, для tolerance fixture-вариаций.
    """
    iso_key = _iso_week_key(week_start)
    csv_path = repo_root / ".metrics" / "rustore_weekly" / f"{iso_key}.csv"
    if not csv_path.exists():
        return (None, {})

    raw = csv_path.read_bytes()
    try:
        text = raw.decode("utf-8")
        # Strip BOM if первый byte отрезан после decode.
        if text.startswith("﻿"):
            text = text[1:]
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"RuStore CSV {csv_path} not UTF-8 readable: {exc}"
            ) from exc

    reader = csv.DictReader(io.StringIO(text))
    header_map = _resolve_header_map(reader.fieldnames)
    if header_map is None:
        raise RuntimeError(
            f"RuStore CSV {csv_path} missing required headers; "
            f"found: {reader.fieldnames}"
        )

    package_norm = package.strip().lower()
    total = 0
    by_country: dict[str, int] = {}

    for row in reader:
        app_raw = row.get(header_map["app"], "")
        if not isinstance(app_raw, str):
            continue
        if app_raw.strip().lower() != package_norm:
            continue

        installs_raw = row.get(header_map["installs"], "0")
        try:
            installs = int((installs_raw or "0").strip() or "0")
        except (ValueError, AttributeError):
            continue
        if installs <= 0:
            continue
        total += installs

        if "country" in header_map:
            country_raw = row.get(header_map["country"], "")
            if isinstance(country_raw, str):
                country = country_raw.strip().upper()
                if country:
                    by_country[country] = by_country.get(country, 0) + installs

    return (total, by_country)


# ===================================================================
# Top country helper (parallel asc/play)
# ===================================================================

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
# Repo root resolution
# ===================================================================

def _repo_root() -> Path:
    """Resolve marketing-v3 repo root from this file's location.

    src/store_metrics/rustore.py → parents[2] == marketing-v3/
    """
    return Path(__file__).resolve().parents[2]


# ===================================================================
# Public API
# ===================================================================

def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Fetch installs (CSV) + rating (API) for one ISO week (RuStore).

    week_start = Monday of the target week (ISO).
    Без всех envs → mock snapshot (preserves CLI behaviour in dev).

    Composition:
        - installs из manual CSV (юзер положил воскресной задачей).
          Если CSV нет → installs=None, error="RuStore CSV не положен...".
        - rating из API (independent of CSV — auth + reviews fetch).
          Если API падает → rating=None, но installs остаются.
    """
    if not _is_configured():
        return StoreSnapshot(
            product=product,
            store="rustore",
            week_start=week_start,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.8 if product == "centry" else 4.7,
            top_country="RU",
            top_country_share=0.95,
        )

    package = _package_for(product)

    # ----- Installs (manual CSV) -----
    csv_error: str | None = None
    installs: int | None = None
    by_country: dict[str, int] = {}
    try:
        installs, by_country = _read_csv_installs(
            _repo_root(), week_start, package,
        )
    except RuntimeError as exc:
        installs = None
        csv_error = f"RuStore CSV error: {exc}"

    csv_missing = installs is None and csv_error is None

    # ----- Rating (API) -----
    rating: float | None = None
    rating_error: str | None = None
    try:
        bearer = _cached_token()
        rating, _count = _fetch_reviews(bearer, package)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully per RESEARCH §5
        rating = None
        rating_error = f"RuStore reviews API: {exc}"
        sys.stderr.write(
            f"WARN: RuStore rating fetch failed for {package}: {exc!r}\n"
        )

    top_cc, share = _top_country(by_country)

    # Compose error string — prefer CSV-side problem over rating-side problem,
    # т.к. csv_missing is most actionable (юзер забыл положить файл).
    if csv_missing:
        error = "RuStore CSV не положен — installs см. Console руками"
    elif csv_error and rating_error:
        error = f"{csv_error}; {rating_error}"
    elif csv_error:
        error = csv_error
    elif rating_error:
        error = rating_error
    else:
        error = None

    return StoreSnapshot(
        product=product,
        store="rustore",
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
            store="rustore",
            week_start=week_start - dt.timedelta(days=7),
            installs=_MOCK_PREV.get(product),
            rating=4.8 if product == "centry" else 4.7,
            top_country="RU",
            top_country_share=0.94,
        )
    prev_week_start = week_start - dt.timedelta(days=7)
    return fetch_weekly(product, prev_week_start)
