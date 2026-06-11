"""Apple App Store metrics adapter — Analytics Reports installs + iTunes RSS ratings.

Pipeline (real-mode, app-id envs set):
    1. **Installs path — ASC Analytics Reports API.** JWT ES256 (Individual
       API Key: header ``{alg,kid=ASC_KEY_ID,typ:JWT}``, payload
       ``{sub:"user", aud:"appstoreconnect-v1", iat, exp}`` — БЕЗ ``iss``,
       БЕЗ ``scope``; проверено реальными вызовами 2026-05-30, см. RESEARCH
       260530-q69). Flow:
         a. find-or-create ONGOING ``analyticsReportRequest`` для app_id
            (идемпотентно — если Apple остановит request за неактивность,
            контур пересоздаст).
         b. ``GET .../reports?filter[name]=App Downloads Standard`` → reportId.
         c. ``GET .../instances?filter[granularity]=DAILY`` → выбрать instances,
            чьи processingDate ∈ нужной ISO-неделе.
         d. ``GET .../segments`` → скачать ``url`` (pre-signed S3, БЕЗ
            Authorization) → gzip.decompress → TSV → суммировать колонку числа.
       ONGOING-инстансы появляются у Apple через **24–48ч** после создания
       request (создан 2026-05-30 для Centry/Diktum). До этого instances
       пусто — это норма, не ошибка: installs=None + понятный ``error``.
    2. _fetch_rss_ratings: GET https://itunes.apple.com/<cc>/rss/customerreviews/
       id=<app_id>/sortBy=mostRecent/page=1/json — no auth, last 50 reviews per
       country, aggregated across RU/US/KZ/BY/UA for weighted avg rating.
       RSS failure → rating=None (soft, doesn't break digest).
    3. fetch_weekly: composes StoreSnapshot from installs (Analytics) + RSS rating.

Graceful degradation (installs axis):
    - Нет ASC_KEY_ID / ASC_PRIVATE_KEY → installs=None + "ключ не настроен".
    - ONGOING request недоступен / отчёт ещё генерируется (24-48ч) → None +
      понятный error.
    - Любая ошибка API / парсинга → None + ``f"ASC installs error: {exc}"``.
    Ни одна ветка НЕ выбрасывает исключение наружу из fetch_weekly —
    дайджест не падает. RSS rating — независимая axis, работает всегда.

Env required (real-mode):
    ASC_APP_ID_CENTRY  — numeric Apple App ID для Centry (RSS + Analytics).
    ASC_APP_ID_DIKTUM  — numeric Apple App ID для Diktum.

Env required для installs (Analytics Reports API):
    ASC_KEY_ID         — Individual ASC API Key ID (e.g. "8SSTB54YPBCY").
    ASC_PRIVATE_KEY    — raw .p8 EC P-256 PEM (multi-line GH Secret).
    (Issuer ID НЕ нужен для Individual Key — голый sub:user работает.)

Без app-id envs → mock data (preserves CLI / dev behaviour). Старые envs
ASC_REPORTER_ACCESS_TOKEN / ASC_VENDOR_NUMBER / ASC_ISSUER_ID больше не
используются — их можно удалить из GH Secrets когда удобно.

References:
    - RESEARCH 260530-q69 — empirically-verified ASC Analytics Reports API
      contract (JWT schema, report name, endpoints, gotchas).
    - Apple — Downloading Analytics Reports.
    - Phase 5 RESEARCH §«iTunes RSS Reviews» — RSS endpoint unchanged.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sys
import time
from typing import Final

import requests

from . import _http
from .models import Product, StoreSnapshot

_RSS_URL_TEMPLATE: Final[str] = (
    "https://itunes.apple.com/{cc}/rss/customerreviews/id={app_id}"
    "/sortBy=mostRecent/page=1/json"
)
_RSS_COUNTRIES: Final[tuple[str, ...]] = ("ru", "us", "kz", "by", "ua")

_MOCK_INSTALLS: dict[Product, int] = {"centry": 23, "diktum": 18, "lucea": 5, "lapulya": 7, "unia": 3}
_MOCK_PREV: dict[Product, int] = {"centry": 19, "diktum": 22, "lucea": 4, "lapulya": 5, "unia": 2}

# Only app-id envs are needed для RSS path.
_REQUIRED_ENVS: Final[tuple[str, ...]] = (
    "ASC_APP_ID_CENTRY",
    "ASC_APP_ID_DIKTUM",
)

# Installs path (Analytics Reports API) gate — отдельно от RSS gate.
_INSTALLS_ENVS: Final[tuple[str, ...]] = (
    "ASC_KEY_ID",
    "ASC_PRIVATE_KEY",
)

# ===================================================================
# ASC Analytics Reports API — constants
# ===================================================================

_ASC_BASE: Final[str] = "https://api.appstoreconnect.apple.com"
_ASC_AUDIENCE: Final[str] = "appstoreconnect-v1"
# JWT lifetime: Apple требует exp ≤ 20 минут, иначе 401. Берём 1200с (20 мин).
_JWT_TTL_SECONDS: Final[int] = 1200
# Пере-генерим токен, если до exp осталось < этого порога.
_JWT_REFRESH_MARGIN_SECONDS: Final[int] = 120

# Отчёт установок (COMMERCE). Standard = агрегированный, минимум колонок.
_INSTALLS_REPORT_NAME: Final[str] = "App Downloads Standard"

# Колонка даты в TSV-сегменте (кандидаты по порядку).
_DATE_COLUMN_CANDIDATES: Final[tuple[str, ...]] = ("Date",)
# Колонка числа установок (кандидаты по порядку — первая совпавшая).
_COUNT_COLUMN_CANDIDATES: Final[tuple[str, ...]] = (
    "Counts",
    "Units",
    "Downloads",
    "Installations",
)

# Error string когда installs-ключ не настроен.
_INSTALLS_NO_KEY_ERROR: Final[str] = (
    "ASC API ключ не настроен (нет ASC_KEY_ID/ASC_PRIVATE_KEY)"
)

# Module-level JWT cache (in-process, single workflow run).
_JWT_CACHE: dict[str, object] = {"token": None, "expires_at": None}


# ===================================================================
# Configuration
# ===================================================================

def _is_configured() -> bool:
    """True iff both ASC_APP_ID_* envs are set (non-empty)."""
    return all(os.environ.get(k) for k in _REQUIRED_ENVS)


def _installs_configured() -> bool:
    """True iff both ASC_KEY_ID и ASC_PRIVATE_KEY заданы (non-empty).

    Отдельный gate от RSS: installs требуют JWT-ключ, ratings — нет.
    """
    return all(os.environ.get(k) for k in _INSTALLS_ENVS)


def _app_id_for(product: Product) -> str:
    """Resolve numeric Apple app id per product, stripping whitespace.

    Key-based (ASC_APP_ID_<KEY>) — поддерживает любое число продуктов. Нет env
    для продукта → raise; gather._collect_stores ловит → StoreSnapshot(
    installs=None, error) (мягкая деградация), остальные продукты не страдают.
    """
    key = f"ASC_APP_ID_{product.upper()}"
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} not set")
    return val


# ===================================================================
# JWT — Individual ASC API Key (ES256, sub:user, no iss/scope)
# ===================================================================

def _asc_jwt() -> str:
    """Build (and cache) ASC Individual-Key JWT.

    Header:  ``{"alg":"ES256","kid":<ASC_KEY_ID>,"typ":"JWT"}``
    Payload: ``{"sub":"user","aud":"appstoreconnect-v1","iat":now,"exp":now+1200}``
    БЕЗ ``iss``, БЕЗ ``scope`` — проверено HTTP 200 (RESEARCH 260530-q69).

    Кэшируется в модульной переменной; пере-генерится если до exp < 120с.
    """
    # Lazy import — heavy crypto only when installs configured.
    import jwt as _jwt

    now = int(time.time())
    cached = _JWT_CACHE.get("token")
    expires_at = _JWT_CACHE.get("expires_at")
    if (
        isinstance(cached, str)
        and cached
        and isinstance(expires_at, int)
        and expires_at - now > _JWT_REFRESH_MARGIN_SECONDS
    ):
        return cached

    key_id = os.environ.get("ASC_KEY_ID", "").strip()
    private_key = os.environ.get("ASC_PRIVATE_KEY", "")
    if not key_id or not private_key:
        raise RuntimeError("ASC_KEY_ID / ASC_PRIVATE_KEY not set")

    exp = now + _JWT_TTL_SECONDS
    headers = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {"sub": "user", "aud": _ASC_AUDIENCE, "iat": now, "exp": exp}
    token = _jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    # PyJWT ≥2 returns str; guard against bytes from exotic versions.
    if isinstance(token, bytes):
        token = token.decode("ascii")

    _JWT_CACHE["token"] = token
    _JWT_CACHE["expires_at"] = exp
    return token


def _reset_jwt_cache() -> None:
    """Test helper — drop cached JWT (called by tests, not production code)."""
    _JWT_CACHE["token"] = None
    _JWT_CACHE["expires_at"] = None


def _asc_get(url: str, params: dict | None = None) -> requests.Response:
    """GET ASC API с Bearer JWT + Accept: application/vnd.api+json.

    Возвращает ``requests.Response`` (4xx НЕ выбрасывает — _http возвращает
    response, caller решает что делать).
    """
    headers = {
        "Authorization": f"Bearer {_asc_jwt()}",
        "Accept": "application/vnd.api+json",
    }
    return _http.fetch_with_retry(
        url=url, method="GET", headers=headers, params=params,
    )


# ===================================================================
# Analytics Reports — find-or-create ONGOING request
# ===================================================================

def _ensure_ongoing_request(app_id: str) -> str | None:
    """Find-or-create ONGOING analyticsReportRequest для app_id.

    1. GET /v1/apps/{app_id}/analyticsReportRequests → ищем существующий
       ``accessType == ONGOING`` и НЕ ``stoppedDueToInactivity`` → вернуть id.
    2. Если нет → POST /v1/analyticsReportRequests (accessType=ONGOING) →
       вернуть новый id.

    Returns:
        requestId (str) или None при ошибке (с stderr WARN).
    """
    list_url = f"{_ASC_BASE}/v1/apps/{app_id}/analyticsReportRequests"
    try:
        resp = _asc_get(list_url)
    except Exception as exc:  # noqa: BLE001 — деградация
        sys.stderr.write(
            f"WARN: ASC analyticsReportRequests list failed for {app_id}: {exc!r}\n"
        )
        return None

    if resp.status_code < 400:
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"WARN: ASC analyticsReportRequests non-JSON for {app_id}: {exc!r}\n"
            )
            payload = {}
        for item in payload.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            if (
                attrs.get("accessType") == "ONGOING"
                and not attrs.get("stoppedDueToInactivity", False)
            ):
                req_id = item.get("id")
                if req_id:
                    return str(req_id)
    else:
        sys.stderr.write(
            f"WARN: ASC analyticsReportRequests list HTTP {resp.status_code} "
            f"for {app_id}\n"
        )

    # Нет существующего ONGOING — создаём.
    create_url = f"{_ASC_BASE}/v1/analyticsReportRequests"
    body = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {"accessType": "ONGOING"},
            "relationships": {
                "app": {"data": {"type": "apps", "id": app_id}},
            },
        }
    }
    headers = {
        "Authorization": f"Bearer {_asc_jwt()}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }
    try:
        create_resp = _http.fetch_with_retry(
            url=create_url, method="POST", headers=headers, json_body=body,
        )
    except Exception as exc:  # noqa: BLE001 — деградация
        sys.stderr.write(
            f"WARN: ASC analyticsReportRequests POST failed for {app_id}: {exc!r}\n"
        )
        return None

    if create_resp.status_code >= 400:
        sys.stderr.write(
            f"WARN: ASC analyticsReportRequests POST HTTP "
            f"{create_resp.status_code} for {app_id}\n"
        )
        return None
    try:
        new_id = create_resp.json().get("data", {}).get("id")
    except (ValueError, json.JSONDecodeError, AttributeError):
        new_id = None
    if not new_id:
        sys.stderr.write(
            f"WARN: ASC analyticsReportRequests POST no id in response "
            f"for {app_id}\n"
        )
        return None
    return str(new_id)


# ===================================================================
# Analytics Reports — TSV segment parsing
# ===================================================================

def _parse_segment_tsv(text: str, target_date: dt.date) -> int:
    """Суммировать колонку числа по строкам, чья дата == target_date (конкретный день instance).

    TSV (Apple analytics — табуляция). Толерантно к мусору: кривые строки
    пропускаются, не валятся.
    """
    lines = text.splitlines()
    if not lines:
        return 0

    header = lines[0].split("\t")
    header_norm = [h.strip() for h in header]

    def _find_col(candidates: tuple[str, ...]) -> int | None:
        for cand in candidates:
            for idx, name in enumerate(header_norm):
                if name == cand:
                    return idx
        return None

    date_idx = _find_col(_DATE_COLUMN_CANDIDATES)
    count_idx = _find_col(_COUNT_COLUMN_CANDIDATES)
    if date_idx is None or count_idx is None:
        sys.stderr.write(
            f"WARN: ASC segment TSV missing date/count column "
            f"(header={header_norm})\n"
        )
        return 0

    total = 0
    for raw in lines[1:]:
        if not raw.strip():
            continue
        cols = raw.split("\t")
        if len(cols) <= max(date_idx, count_idx):
            continue
        date_str = cols[date_idx].strip()
        try:
            row_date = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        if row_date != target_date:
            continue
        count_str = cols[count_idx].strip().replace(",", "")
        try:
            total += int(count_str)
        except ValueError:
            # Может быть float-строкой ("12.0") — толерантно.
            try:
                total += int(float(count_str))
            except ValueError:
                continue
    return total


def _download_segment(url: str) -> str:
    """GET pre-signed segment url БЕЗ Authorization → gzip.decompress → utf-8.

    segment.url — самодостаточная S3-style ссылка; Authorization Bearer
    на неё слать НЕЛЬЗЯ (ошибка). Тело gzip-сжато.
    """
    resp = requests.get(url, timeout=_http.REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    raw = resp.content
    try:
        decompressed = gzip.decompress(raw)
    except (OSError, EOFError):
        # Не gzip (на всякий) — пробуем как plain bytes.
        decompressed = raw
    return decompressed.decode("utf-8", errors="replace")


# ===================================================================
# Analytics Reports — main installs fetch
# ===================================================================

def _fetch_installs_range(
    app_id: str, start_date: dt.date, end_date: dt.date,
) -> tuple[int | None, str | None]:
    """Получить installs (App Store downloads) за произвольный диапазон дат.

    Диапазон [start_date, end_date] включительно — DAILY instances фильтруются
    по processingDate ∈ диапазону, каждый instance суммируется per-day через
    :func:`_parse_segment_tsv` (per-instance target_date, fix 260611-7sc).

    Returns:
        (installs, error). installs=None при любой не-успешной ветке;
        error — человекочитаемое сообщение (None при успехе).

    Never raises — любое исключение ловится → (None, "ASC installs error: ...").
    """
    try:
        request_id = _ensure_ongoing_request(app_id)
        if not request_id:
            return (None, "ASC: не удалось получить ONGOING request")

        # --- reports: filter[name]=App Downloads Standard ---
        reports_url = (
            f"{_ASC_BASE}/v1/analyticsReportRequests/{request_id}/reports"
        )
        reports_resp = _asc_get(
            reports_url,
            params={"filter[name]": _INSTALLS_REPORT_NAME, "limit": 10},
        )
        if reports_resp.status_code >= 400:
            return (
                None,
                f"ASC: reports HTTP {reports_resp.status_code}",
            )
        reports_data = (reports_resp.json() or {}).get("data", []) or []
        if not reports_data:
            return (None, "ASC: отчёт App Downloads Standard недоступен")
        report_id = reports_data[0].get("id")
        if not report_id:
            return (None, "ASC: отчёт App Downloads Standard недоступен")

        # --- instances: filter[granularity]=DAILY, filter by processingDate ---
        instances_url = (
            f"{_ASC_BASE}/v1/analyticsReports/{report_id}/instances"
        )
        instances_resp = _asc_get(
            instances_url, params={"filter[granularity]": "DAILY"},
        )
        if instances_resp.status_code >= 400:
            return (
                None,
                f"ASC: instances HTTP {instances_resp.status_code}",
            )
        all_instances = (instances_resp.json() or {}).get("data", []) or []

        wanted_instances: list[tuple[str, dt.date]] = []
        for inst in all_instances:
            if not isinstance(inst, dict):
                continue
            attrs = inst.get("attributes") or {}
            proc_str = attrs.get("processingDate")
            if not proc_str:
                continue
            try:
                proc_date = dt.date.fromisoformat(str(proc_str))
            except ValueError:
                continue
            if start_date <= proc_date <= end_date:
                inst_id = inst.get("id")
                if inst_id:
                    wanted_instances.append((str(inst_id), proc_date))

        if not wanted_instances:
            return (
                None,
                "ASC: отчёт ещё генерируется (instances пусто; "
                "ONGOING создан 30.05, ждём 24-48ч)",
            )

        # --- segments → download → TSV → sum ---
        total_installs = 0
        for inst_id, proc_date in wanted_instances:
            seg_url = (
                f"{_ASC_BASE}/v1/analyticsReportInstances/{inst_id}/segments"
            )
            seg_resp = _asc_get(seg_url)
            if seg_resp.status_code >= 400:
                sys.stderr.write(
                    f"WARN: ASC segments HTTP {seg_resp.status_code} "
                    f"for instance {inst_id}\n"
                )
                continue
            segments = (seg_resp.json() or {}).get("data", []) or []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                seg_attrs = seg.get("attributes") or {}
                download_url = seg_attrs.get("url")
                if not download_url:
                    continue
                tsv_text = _download_segment(download_url)
                total_installs += _parse_segment_tsv(tsv_text, proc_date)

        return (total_installs, None)

    except Exception as exc:  # noqa: BLE001 — никогда не падаем наружу
        sys.stderr.write(f"WARN: ASC installs error for {app_id}: {exc!r}\n")
        return (None, f"ASC installs error: {exc}")


def _fetch_installs(
    app_id: str, week_start: dt.date,
) -> tuple[int | None, str | None]:
    """Получить installs за ISO-неделю — тонкая обёртка над range-версией.

    Контракт сохранён для недельного контура (hybrid_report) и тестов:
    [week_start, week_start + 6 дней] включительно.
    """
    return _fetch_installs_range(
        app_id, week_start, week_start + dt.timedelta(days=6),
    )


def _month_range(year: int, month: int) -> tuple[dt.date, dt.date]:
    """(первое число, последнее число) календарного месяца."""
    first = dt.date(year, month, 1)
    last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return first, last


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
    """Fetch installs (Analytics Reports) + rating (RSS) for one ISO week.

    week_start = Monday of the target week (ISO).
    Без app-id envs → mock snapshot (preserves CLI behaviour).

    Composition:
        - installs из ASC Analytics Reports API (если ASC_KEY_ID/
          ASC_PRIVATE_KEY заданы). Нет ключа / отчёт ещё генерируется
          (24-48ч после создания ONGOING) / ошибка API → installs=None
          с понятным ``error``. Никогда не падает наружу.
        - rating из iTunes RSS (no auth, no blocking). RSS failure →
          rating=None (отдельная axis, installs не зависит).
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

    try:
        app_id = _app_id_for(product)
    except RuntimeError:
        return StoreSnapshot(
            product=product,
            store="app_store",
            week_start=week_start,
            installs=None,
            error=f"ASC_APP_ID_{product.upper()} не задан — добавьте в GH Secrets",
        )

    # ----- Installs (ASC Analytics Reports API) -----
    if _installs_configured():
        installs, installs_error = _fetch_installs(app_id, week_start)
    else:
        installs, installs_error = None, _INSTALLS_NO_KEY_ERROR

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


def fetch_monthly(product: Product, year: int, month: int) -> StoreSnapshot:
    """Fetch installs (Analytics Reports) + rating (RSS) за календарный месяц.

    Зеркало :func:`fetch_weekly`, но диапазон = [1-е, последнее число месяца].
    ``week_start`` снапшота = первое число месяца (семантика «начало периода»).
    Never raises — деградация идентична недельной.
    """
    month_first, month_last = _month_range(year, month)

    if not _is_configured():
        return StoreSnapshot(
            product=product,
            store="app_store",
            week_start=month_first,
            installs=_MOCK_INSTALLS.get(product),
            rating=4.7 if product == "centry" else 4.6,
            top_country="RU",
            top_country_share=0.78,
        )

    try:
        app_id = _app_id_for(product)
    except RuntimeError:
        return StoreSnapshot(
            product=product,
            store="app_store",
            week_start=month_first,
            installs=None,
            error=f"ASC_APP_ID_{product.upper()} не задан — добавьте в GH Secrets",
        )

    # ----- Installs (ASC Analytics Reports API, месячный диапазон) -----
    if _installs_configured():
        installs, installs_error = _fetch_installs_range(
            app_id, month_first, month_last,
        )
    else:
        installs, installs_error = None, _INSTALLS_NO_KEY_ERROR

    # ----- Ratings (iTunes RSS) -----
    rating: float | None = None
    try:
        rating, _count = _fetch_rss_ratings(app_id)
    except Exception as exc:  # noqa: BLE001 — RSS не критичен для отчёта
        sys.stderr.write(f"WARN: ASC RSS fetch failed for {app_id}: {exc!r}\n")
        rating = None

    return StoreSnapshot(
        product=product,
        store="app_store",
        week_start=month_first,
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
