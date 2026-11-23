"""Сводка оценок по продуктам × сторам → ``.metrics/ratings_summary.json``.

quick 261111-rts: рейтинг в шапке карточек дашборда студии (fortonlab.ru/dash).
Дашборд читает этот файл из публичного репо (raw.githubusercontent.com) — в нём
ТОЛЬКО числа, которые и так видны на страницах сторов: средняя, число оценок,
число отзывов с текстом, разбивка 5→1. Ни авторов, ни текстов.

Источники (всё — то, что видит любой посетитель стора, либо наш API):
    * App Store — публичная страница apps.apple.com/<cc>/app/id<id>: блок
      ``"totalNumberOfRatings"…"ratingCounts":[5★..1★]`` по каждой стране из
      ``asc._RSS_COUNTRIES``; страны суммируются. Отзывы с текстом — число id
      в seen-state бота (копится с запуска бота).
    * Google Play — публичная страница play.google.com (``hl=ru&gl=RU``): блок
      ``{"52":[["4,7",4.71],[null,[1★],[2★],[3★],[4★],[5★]],[оценок],[отзывов]``.
      API Google не отдаёт ни общего числа, ни гистограммы. Пока оценок мало,
      Google их не показывает — стор просто отсутствует в сводке.
    * RuStore — ``rustore.fetch_rating_stats`` (/comment/statistic, наш API):
      всё точно; отзывы с текстом = всего − без текста.

Стор, который в этом прогоне не ответил, сохраняет прошлое значение (со своим
``updated_at``) — дашборд не мигает. Модуль никогда не падает наружу.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import Final

from src.store_metrics import _http, asc, play, rustore

SUMMARY_NAME: Final[str] = "ratings_summary.json"
AppStorePages = dict[str, Future[dict[int, int] | None]]
_UA: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
_AS_PAGE: Final[str] = "https://apps.apple.com/{cc}/app/id{app_id}"
_GP_PAGE: Final[str] = "https://play.google.com/store/apps/details?id={pkg}&hl=ru&gl=RU"

_AS_RE: Final[re.Pattern[str]] = re.compile(
    r'"totalNumberOfRatings":(\d+)[^{}\[]*"ratingCounts":\[(\d+),(\d+),(\d+),(\d+),(\d+)\]'
)
_N: Final[str] = r'\["[^"]*",(\d+)\]'
_GP_RE: Final[re.Pattern[str]] = re.compile(
    r'\{"52":\[\["[^"]*",[\d.]+\],\[null,' + ",".join([_N] * 5) + r"\]," + _N + "," + _N
)


def _stars_entry(stars: dict[int, int], reviews: int | None) -> dict | None:
    """Запись стора из гистограммы {1..5: n}. Нет оценок → None."""
    total = sum(stars.values())
    if total <= 0:
        return None
    avg = sum(s * n for s, n in stars.items()) / total
    return {
        "avg": round(avg, 2),
        "ratings": total,
        "reviews": reviews,
        "stars": {str(s): int(stars.get(s, 0)) for s in (5, 4, 3, 2, 1)},
    }


def parse_app_store_page(html: str) -> dict[int, int] | None:
    """Гистограмма {1..5: n} со страницы App Store одной страны | None."""
    m = _AS_RE.search(html)
    if not m:
        return None
    total, *counts = (int(x) for x in m.groups())
    hist = {5 - i: n for i, n in enumerate(counts)}  # ratingCounts: 5★ → 1★
    if sum(hist.values()) != total:
        return None
    return hist


def parse_google_play_page(html: str) -> tuple[dict[int, int], int, int] | None:
    """(гистограмма {1..5: n}, оценок, отзывов) со страницы Google Play | None."""
    m = _GP_RE.search(html)
    if not m:
        return None
    nums = [int(x) for x in m.groups()]
    hist = {s: nums[s - 1] for s in range(1, 6)}
    return hist, nums[5], nums[6]


def _get(url: str) -> str | None:
    try:
        resp = _http.fetch_with_retry(url=url, method="GET", headers=_UA)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"INFO: ratings summary GET failed {url}: {exc!r}\n")
        return None
    if resp.status_code >= 400:
        return None
    return resp.text


def _fetch_app_store_page(app_id: str, country: str) -> dict[int, int] | None:
    html = _get(_AS_PAGE.format(cc=country, app_id=app_id))
    return parse_app_store_page(html) if html else None


def prefetch_app_store_pages(executor: Executor, products: list[str]) -> dict[str, AppStorePages]:
    """Запустить запросы сводки параллельно с опросом уведомлений."""
    pages: dict[str, AppStorePages] = {}
    for product in products:
        try:
            app_id = asc._app_id_for(product)  # type: ignore[arg-type]
        except (ValueError, RuntimeError):
            continue
        pages[product] = {
            country: executor.submit(_fetch_app_store_page, app_id, country)
            for country in asc._RSS_COUNTRIES
        }
    return pages


def read_app_store_page(
    page: Future[dict[int, int] | None] | None, *, wait: bool = False,
) -> dict[int, int] | None:
    if page is None or (not wait and not page.done()):
        return None
    try:
        return page.result()
    except Exception as exc:  # noqa: BLE001 — сбой одной витрины не блокирует остальные
        sys.stderr.write(f"INFO: App Store storefront skipped: {exc!r}\n")
        return None


def collect_app_store(
    product: str, seen: dict, pages: AppStorePages | None = None,
) -> dict | None:
    if pages is None:
        app_id = asc._app_id_for(product)  # type: ignore[arg-type]
        histograms = (_fetch_app_store_page(app_id, country) for country in asc._RSS_COUNTRIES)
    else:
        histograms = (read_app_store_page(page, wait=True) for page in pages.values())

    total: dict[int, int] = {s: 0 for s in range(1, 6)}
    answered = False
    for hist in histograms:
        if hist is None:
            continue
        answered = True
        for s, n in hist.items():
            total[s] += n
    if not answered:
        return None
    reviews = len((seen.get(product) or {}).get("app_store") or [])
    return _stars_entry(total, reviews) or {"empty": True}


def collect_google_play(product: str) -> dict | None:
    if not play._is_configured():
        return None
    pkg = play._package_for(product)  # type: ignore[arg-type]
    html = _get(_GP_PAGE.format(pkg=pkg))
    if not html:
        return None
    parsed = parse_google_play_page(html)
    if parsed is None:
        # Страница есть, но Google ещё не показывает рейтинг (мало оценок).
        return {"empty": True}
    hist, ratings, reviews = parsed
    entry = _stars_entry(hist, reviews)
    if entry is None:
        return {"empty": True}
    # Google показывает число оценок чуть больше суммы гистограммы (свой
    # пересчёт) — берём то, что видит посетитель стора.
    entry["ratings"] = max(entry["ratings"], ratings)
    return entry


def collect_rustore(product: str) -> dict | None:
    if not rustore._is_configured():
        return None
    pkg = rustore._package_for(product)  # type: ignore[arg-type]
    stats = rustore.fetch_rating_stats(rustore._cached_token(), pkg)
    if stats is None:
        return None
    hist = {s: int(stats["per_star"].get(s, 0)) for s in range(1, 6)}
    reviews = max(0, int(stats.get("total") or 0) - int(stats.get("no_comments") or 0))
    return _stars_entry(hist, reviews) or {"empty": True}


def _collect(product: str, store: str, seen: dict) -> dict | None:
    if store == "app_store":
        return collect_app_store(product, seen)
    if store == "google_play":
        return collect_google_play(product)
    return collect_rustore(product)


STORES: Final[tuple[str, ...]] = ("app_store", "google_play", "rustore")


def merge(prev: dict, fresh: dict[str, dict[str, dict | None]], now: str) -> dict:
    """Свежие значения поверх прошлых. ``None`` (стор не ответил) — прошлое
    значение остаётся; ``{"empty": True}`` (оценок нет) — стор убирается."""
    products: dict = {
        p: dict(v) for p, v in (prev.get("products") or {}).items() if isinstance(v, dict)
    }
    changed = False
    for product, stores in fresh.items():
        bucket = products.setdefault(product, {})
        for store, entry in stores.items():
            if entry is None:
                continue
            old = bucket.get(store)
            if entry.get("empty"):
                if old is not None:
                    bucket.pop(store)
                    changed = True
                continue
            old_values = {k: v for k, v in (old or {}).items() if k != "updated_at"}
            if old_values != entry:
                # updated_at сдвигается только при изменении — иначе бот
                # коммитил бы файл каждый час без новых данных.
                bucket[store] = dict(entry, updated_at=now)
                changed = True
    products = {p: v for p, v in products.items() if v}
    return {"updated_at": now if changed else prev.get("updated_at", now), "products": products}


def write_summary(
    path: Path, products: list[str], seen: dict, *,
    app_store_pages: dict[str, AppStorePages] | None = None,
) -> None:
    """Собрать сводку и записать файл. Никогда не падает наружу."""
    try:
        prev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (ValueError, OSError):
        prev = {}
    if not isinstance(prev, dict):
        prev = {}
    fresh: dict[str, dict[str, dict | None]] = {}
    for product in products:
        fresh[product] = {}
        for store in STORES:
            try:
                if store == "app_store" and app_store_pages is not None:
                    entry = collect_app_store(product, seen, app_store_pages.get(product, {}))
                else:
                    entry = _collect(product, store, seen)
                fresh[product][store] = entry
            except Exception as exc:  # noqa: BLE001 — нестроенный стор и т.п.
                sys.stderr.write(f"INFO: ratings summary {product}/{store}: {exc!r}\n")
                fresh[product][store] = None
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    data = merge(prev, fresh, now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        sys.stderr.write(f"WARN: ratings summary not written: {exc!r}\n")
