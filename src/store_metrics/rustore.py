"""RuStore metrics adapter — JWS RSA-SHA512 auth + reviews API + installs stub.

Pipeline (real-mode, all envs set):
    1. _authenticate: POST /public/auth/ с JSON {keyId, timestamp, signature}.
       signature = base64(RSA-SHA512-PKCS1v15 over (keyId+timestamp)).
       Response body.jwe = bearer token (TTL 900 sec).
    2. _cached_token: module-level cache, перевыпускаем токен при истечении 870с
       safety margin (single workflow run = ~30 сек, одной авторизации хватит).
    3. _fetch_reviews: GET /public/v1/application/<packageName>/comment
       Header: Public-Token: <token>. Pagination via page/size + body.last.
       Filter commentStatus == "PUBLISHED", aggregate appRating 1-5.
    4. **Installs path — stubbed.** RuStore Public API НЕ предоставляет
       installs/stats endpoints — это constraint от Mail.ru (Brain decision
       Q3 2026-05-14, verified through full list of 9 Console methods).
       Возвращаем installs=None + явный error-message.
    5. fetch_weekly: композирует StoreSnapshot из stub installs + API rating.

Architectural notes:
    - RuStore Public API НЕ отдаёт installs (Brain decision 2026-05-14,
      verified UI Console — 9 методов "Общие методы", нет statistics).
      Это constraint Mail.ru, не наш wire issue — installs физически
      невозможны автоматически без HTML scraping fragile Console UI.
    - Manual CSV mode был отвергнут юзером (2026-05-15: "я не буду ничего
      загружать руками"). Canonical solution для installs — ждать пока
      Mail.ru добавит statistics endpoint в Public API.
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
    - Phase 5 RESEARCH §5 «RuStore Authorization» / §6 «Reviews»
    - Brain decisions 2026-05-14 «RuStore API статистики НЕТ → Q3 закрыт»
    - Brain decisions 2026-05-15 «full-auto mode, drop manual CSV»
"""
from __future__ import annotations

import base64
import datetime as dt
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

# Stable error string — surfaced в StoreSnapshot.error для installs.
# Mail.ru constraint: Public API не отдаёт installs endpoint. Меняется
# ТОЛЬКО когда Mail.ru добавит statistics endpoint в API.
_INSTALLS_LIMITATION_ERROR: Final[str] = (
    "RuStore Public API не отдаёт installs (Mail.ru ограничение, "
    "Brain Q3 2026-05-14). Альтернативы: HTML scrape (fragile) или "
    "ждать пока Mail.ru добавит stats endpoint."
)


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
    """Resolve RuStore packageName per product (same identifiers as GPlay).

    HOTFIX 2026-05-15: strip env values — GH Secret storage may include
    trailing whitespace that breaks URL path matching at the reviews endpoint.
    """
    key = "RUSTORE_PACKAGE_CENTRY" if product == "centry" else "RUSTORE_PACKAGE_DIKTUM"
    val = os.environ.get(key, "").strip()
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
    # HOTFIX 2026-05-15 (smoke test run 25890122345): RuStore auth returned
    # HTTP 400 "Invalid request format. Unexpected value". Likely cause:
    # GH Secret values include trailing whitespace/newline which breaks
    # signature (signed message becomes "<key_id>\n<timestamp>" while server
    # signs over "<key_id><timestamp>" → mismatch). Strip on read.
    key_id = os.environ["RUSTORE_KEY_ID"].strip()
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
        # HOTFIX 2026-05-15 #2: smoke run 25891726581 still showed HTTP 400
        # after .strip(). Surface key_id repr + timestamp + signature length
        # so we can see exact body sent (no secret leak — key_id is public
        # identifier, signature truncated).
        raise RuntimeError(
            f"RuStore auth HTTP {resp.status_code}: {resp.text[:300]} "
            f"| sent keyId={key_id!r} timestamp={timestamp!r} "
            f"signature_len={len(signature)}"
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
# Public API
# ===================================================================

def fetch_weekly(product: Product, week_start: dt.date) -> StoreSnapshot:
    """Fetch installs (stub) + rating (JWS API) for one ISO week (RuStore).

    week_start = Monday of the target week (ISO).
    Без всех envs → mock snapshot (preserves CLI behaviour in dev).

    Composition:
        - installs = None (всегда) + error mentioning Mail.ru limitation.
          RuStore Public API физически не отдаёт installs endpoint —
          ждём пока Mail.ru добавит. До этого момента installs всегда None.
        - rating через JWS auth + reviews API.
          Если API падает → rating=None, error appended после installs-blocker.
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

    # ----- Installs (RuStore Public API — Mail.ru limitation) -----
    # RuStore Public API НЕ предоставляет installs/stats endpoints
    # (Brain decision Q3 2026-05-14, документально подтверждено через
    # обзор полного списка методов в Console). Это constraint от Mail.ru,
    # не наш wire issue — installs физически невозможны автоматически.
    installs: int | None = None
    by_country: dict[str, int] = {}  # noqa: F841 — placeholder если Mail.ru добавит endpoint
    installs_error = _INSTALLS_LIMITATION_ERROR

    # ----- Rating (JWS reviews API) -----
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

    # Compose error string — installs blocker всегда присутствует, rating
    # error дописывается если был.
    if rating_error:
        error = f"{installs_error}; {rating_error}"
    else:
        error = installs_error

    return StoreSnapshot(
        product=product,
        store="rustore",
        week_start=week_start,
        installs=installs,
        uninstalls=None,
        rating=rating,
        rating_count=None,
        top_country=None,
        top_country_share=None,
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
