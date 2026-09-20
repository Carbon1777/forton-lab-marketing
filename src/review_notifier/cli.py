"""Review notifier — поллер новых отзывов из сторов → TG-карточки.

quick 260626-ozg.

Несколько раз в день (cron в GH Actions) тянет per-review списки по 6 продуктам
× 3 сторам через ``store_metrics.{asc,play,rustore}.fetch_reviews_list``,
определяет НОВЫЕ отзывы (которых ещё не видел через ``.metrics/reviews_seen.json``)
и шлёт каждый новый отзыв отдельной HTML-карточкой в TG-канал «Планировщик».

Контракты:
    - load_seen / save_seen — JSON ``{"<product>": {"<store>": ["<review_id>", ...]}}``.
    - find_new — BASELINE при первом появлении пары product+store (засев без
      рассылки); иначе новые = отзывы, чьих id нет в seen-списке.
    - update_seen — добавляет все id текущего прогона (порядок, без дублей),
      обрезает до ``MAX_SEEN_PER_PAIR`` с головы (старые), новые в конце — prune
      НЕ приводит к повторной отправке (засев ДО обрезки).
    - format_card — HTML с ``html.escape`` пользовательского author/text.
    - send_card — POST sendMessage, never raises, возвращает bool.
    - _collect_reviews — собирает по трём сторам, нестроенный стор мягко
      пропускается (raise _app_id_for/_package_for / _is_configured()==False).
    - main — оркестрация: baseline не шлёт, шлёт ровно новые, падение одной
      карточки не валит остальные.

Оценки без текста (quick 260917-g60):
    - App Store — iTunes lookup ``userRatingCount``/``averageUserRating`` по
      стране минус текстовые отзывы RSS этой страны.
    - RuStore — ``/comment/statistic`` (``ratingsNoComments`` + per-star).
    - Google Play — НЕВОЗМОЖНО: API отдаёт только отзывы с текстом, а
      GCS-отчёты reviews/ratings сервис-аккаунту недоступны.
    Состояние — watermark в ``.metrics/ratings_seen.json``. Карточка App Store
    только когда выросли ОБА счётчика: и «без текста» (lookup − RSS), и общее
    число оценок; это гасит и лаг RSS↔lookup, и пустую ленту RSS при живых
    отзывах. Первый прогон (и миграция схемы) — baseline без рассылки.

Мягкая деградация: отсутствие секретов / сети → всё мягко пропускается,
исключение наружу не выбрасывается.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import sys
from pathlib import Path
from typing import Final

import requests

from src.store_metrics import asc, play, rustore
from src.store_metrics.models import Product

PRODUCTS: Final[list[Product]] = [
    "centry", "diktum", "lucea", "lapulya", "unia", "listvia",
]
SEEN_PATH: Final[Path] = Path(".metrics/reviews_seen.json")
# Состояние счётчиков оценок без текста — рядом с seen (тот же каталог).
RATINGS_STATE_NAME: Final[str] = "ratings_seen.json"
MAX_SEEN_PER_PAIR: Final[int] = 500
# «Новый» отзыв старше этого — засеивается молча (напр. после починки
# RuStore-парсера или сброса seen не должны улететь отзывы из прошлого).
MAX_REVIEW_AGE_DAYS: Final[int] = 14
STORES: Final[tuple[str, ...]] = ("app_store", "google_play", "rustore")
STORE_LABELS: Final[dict[str, str]] = {
    "app_store": "App Store",
    "google_play": "Google Play",
    "rustore": "RuStore",
}
PRODUCT_LABELS: Final[dict[str, str]] = {
    "centry": "Centry",
    "diktum": "Diktum",
    "lucea": "Lucea",
    "lapulya": "Лапуля",
    "unia": "Unia",
    "listvia": "Листвия",
}


# ===================================================================
# Seen-state persistence (.metrics/reviews_seen.json)
# ===================================================================

def load_seen(path: Path) -> dict:
    """Загрузить seen dict; пустой если файла нет / он невалиден."""
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_seen(path: Path, data: dict) -> None:
    """Записать seen dict (pretty json, mkdir parents)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


# ===================================================================
# Dedup / baseline / prune
# ===================================================================

def find_new(
    seen: dict, product: str, store: str, reviews: list[dict],
) -> tuple[list[dict], bool]:
    """Определить новые отзывы + baseline-флаг.

    baseline = пара product+store ещё НЕ виделась (ключа нет в seen). В baseline
    — рассылки нет (вернуть []), только засев. Иначе новые = отзывы, чьих
    review_id нет в seen[product][store] (даже если список пустой — стор уже
    виделся).
    """
    baseline = (product not in seen) or (store not in seen.get(product, {}))
    if baseline:
        return ([], True)
    seen_ids = set(seen[product][store])
    new = [r for r in reviews if r["review_id"] not in seen_ids]
    return (new, False)


def update_seen(
    seen: dict, product: str, store: str, reviews: list[dict],
) -> None:
    """Засеять/обновить seen всеми id текущего прогона, затем обрезать.

    Порядок сохраняется, дубли убираются, новые id добавляются в конец. Обрезка
    до ``MAX_SEEN_PER_PAIR`` с головы (старые) — ПОСЛЕ засева, поэтому ранее
    виденный id не станет «новым» в следующем прогоне. Гарантирует создание
    ключей product/store (выход из baseline).
    """
    prod_bucket = seen.setdefault(product, {})
    existing: list[str] = list(prod_bucket.get(store, []))
    existing_set = set(existing)
    for r in reviews:
        rid = r["review_id"]
        if rid not in existing_set:
            existing.append(rid)
            existing_set.add(rid)
    if len(existing) > MAX_SEEN_PER_PAIR:
        existing = existing[-MAX_SEEN_PER_PAIR:]
    prod_bucket[store] = existing


# ===================================================================
# Card formatting + sending
# ===================================================================

def format_card(review: dict, product: str) -> str:
    """HTML-карточка одного отзыва (parse_mode HTML).

    Пользовательские author/text экранируются через ``html.escape``. Рейтинг —
    ⭐ по числу звёзд. Дата выводится если есть.
    """
    rating = review.get("rating") or 0
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        rating = 0
    stars = "⭐" * max(0, min(5, rating))
    product_label = PRODUCT_LABELS.get(product, product)
    store_label = STORE_LABELS.get(review.get("store", ""), review.get("store", ""))
    author = html.escape(str(review.get("author") or "Аноним"))
    text = html.escape(str(review.get("text") or ""))
    date = review.get("date")

    lines = [
        f"📝 <b>Новый отзыв</b> · {html.escape(product_label)} · "
        f"{html.escape(store_label)}",
        f"{stars} ({rating}/5) · {author}",
    ]
    if text:
        lines.append("")
        lines.append(text)
    if date:
        lines.append("")
        lines.append(f"🕒 {html.escape(str(date))}")
    return "\n".join(lines)


def send_card(card: str) -> bool:
    """POST sendMessage в TG-канал «Планировщик». Never raises → bool.

    Env: TG_PLANNER_BOT_TOKEN + TG_OWNER_CHAT_ID. Нет creds → False + WARN.
    """
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — карточка не отправлена\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": card,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        sys.stderr.write(
            f"ERROR: TG sendMessage HTTP {r.status_code}: {r.text[:200]}\n"
        )
        return False
    except requests.RequestException as exc:
        sys.stderr.write(f"ERROR: TG send failed: {exc!r}\n")
        return False


def _parse_date(value: object) -> dt.datetime | None:
    """ISO-дата/датавремя отзыва → aware datetime (UTC по умолчанию) | None."""
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    for candidate in (raw, raw.replace(" ", "T", 1), raw[:10]):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None


def is_fresh(review: dict, now: dt.datetime | None = None) -> bool:
    """True если отзыв моложе ``MAX_REVIEW_AGE_DAYS`` или дата неизвестна."""
    parsed = _parse_date(review.get("date"))
    if parsed is None:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return now - parsed <= dt.timedelta(days=MAX_REVIEW_AGE_DAYS)


# ===================================================================
# Ratings without text — watermark diff + card
# ===================================================================

def compute_app_store_rating_events(
    state: dict | None,
    counts: dict[str, dict],
    rss_by_country: dict[str, list[dict] | None],
) -> tuple[list[dict], dict]:
    """Новые оценки без текста в App Store по странам.

    state: ``{cc: {"no_text", "no_text_sum", "count", "sum"}}`` (None = baseline).
    counts: ``{cc: {"count", "avg"}}`` из iTunes lookup.
    rss_by_country: текстовые отзывы по стране; ``None`` = RSS не ответил →
    страна пропускается.

    Два независимых источника, у каждого свой лаг и свои сбои:
      * lookup — общее число оценок (растёт и от оценок, и от отзывов);
      * RSS — только отзывы с текстом, лента бывает ПУСТОЙ при живых отзывах
        (verified 2026-09-20: Diktum RU отдал 0 вместо 3 → «+3 новые оценки»,
        хотя общее число оценок не менялось).

    Поэтому событие = ``min`` прироста двух величин: оценок без текста
    (count − отзывы RSS) И общего числа оценок. Пустая лента RSS не создаёт
    события (общее число не выросло), а lookup, обогнавший RSS, — тоже
    (не выросло число без текста). Watermark сдвигается ровно на размер
    события, чтобы всплеск RSS не «съедал» будущие настоящие оценки.
    """
    new_state = {cc: dict(v) for cc, v in (state or {}).items()}
    events: list[dict] = []
    for cc, c in counts.items():
        reviews = rss_by_country.get(cc)
        if reviews is None:
            continue
        count = int(c.get("count") or 0)
        avg = float(c.get("avg") or 0.0)
        total_sum = round(avg * count)
        no_text = max(0, count - len(reviews))
        no_text_sum = max(0, total_sum - sum(int(r.get("rating") or 0) for r in reviews))
        prev = new_state.get(cc)
        # Baseline / миграция со старой схемы (без "count") — засев без рассылки.
        if prev is None or "count" not in prev:
            new_state[cc] = {
                "no_text": no_text, "no_text_sum": no_text_sum,
                "count": count, "sum": total_sum,
            }
            continue

        prev_no_text = int(prev.get("no_text") or 0)
        prev_no_text_sum = int(prev.get("no_text_sum") or 0)
        prev_count = int(prev.get("count") or 0)
        delta = min(no_text - prev_no_text, count - prev_count)
        if delta <= 0:
            # Ничего нового: watermark'и только вверх (count), no_text не трогаем.
            new_state[cc] = {
                "no_text": prev_no_text, "no_text_sum": prev_no_text_sum,
                "count": max(prev_count, count), "sum": max(int(prev.get("sum") or 0), total_sum),
            }
            continue

        sum_delta = no_text_sum - prev_no_text_sum
        plausible = delta <= sum_delta <= 5 * delta
        stars: list[int] | None = None
        new_avg: float | None = None
        if plausible and delta == 1:
            stars = [sum_delta]
        elif plausible:
            new_avg = round(sum_delta / delta, 1)
        events.append({
            "store": "app_store", "country": cc, "delta": delta,
            "stars": stars, "new_avg": new_avg,
            "total": count, "avg": avg,
        })
        # Сдвиг ровно на событие (а не на текущий срез) — иначе пустая лента RSS
        # задрала бы watermark и следующая реальная оценка потерялась бы.
        applied_sum = sum_delta if plausible else max(delta, min(5 * delta, round(avg * delta)))
        new_state[cc] = {
            "no_text": prev_no_text + delta,
            "no_text_sum": prev_no_text_sum + applied_sum,
            "count": max(prev_count, count),
            "sum": max(int(prev.get("sum") or 0), total_sum),
        }
    return events, new_state


def compute_rustore_rating_events(
    state: dict | None, stats: dict, text_reviews: list[dict],
) -> tuple[list[dict], dict]:
    """Новые оценки без текста в RuStore.

    state: ``{"no_comments": int, "per_star_no_text": {"1".."5": int}}``.
    stats: :func:`rustore.fetch_rating_stats`. Событие — только при росте
    ``ratingsNoComments`` выше watermark; звёзды — по приросту per-star
    (amount минус текстовые отзывы этой звезды), если он сходится с дельтой.
    """
    text_by_star = {s: 0 for s in range(1, 6)}
    for r in text_reviews:
        star = int(r.get("rating") or 0)
        if star in text_by_star:
            text_by_star[star] += 1
    per_star_no_text = {
        str(s): max(0, int(stats["per_star"].get(s, 0)) - text_by_star[s])
        for s in range(1, 6)
    }
    no_comments = int(stats.get("no_comments") or 0)
    new_state = {"no_comments": no_comments, "per_star_no_text": per_star_no_text}
    if state is None:
        return [], new_state

    prev_no = int(state.get("no_comments") or 0)
    if no_comments <= prev_no:
        # watermark держим, per-star обновляем (самолечение атрибуции звёзд).
        new_state["no_comments"] = prev_no
        return [], new_state

    delta = no_comments - prev_no
    prev_star = state.get("per_star_no_text") or {}
    stars: list[int] = []
    for s in range(1, 6):
        inc = per_star_no_text[str(s)] - int(prev_star.get(str(s)) or 0)
        stars.extend([s] * max(0, inc))
    event = {
        "store": "rustore", "country": None, "delta": delta,
        "stars": sorted(stars, reverse=True) if len(stars) == delta else None,
        "new_avg": None,
        "total": int(stats.get("total") or 0), "avg": float(stats.get("avg") or 0.0),
    }
    return [event], new_state


def _plural_ratings(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "новая оценка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "новые оценки"
    return "новых оценок"


def format_rating_card(event: dict, product: str) -> str:
    """HTML-карточка новой оценки без текста."""
    product_label = PRODUCT_LABELS.get(product, product)
    store_label = STORE_LABELS.get(event["store"], event["store"])
    if event.get("country"):
        store_label = f"{store_label} ({event['country'].upper()})"
    delta = int(event["delta"])
    head = "Новая оценка" if delta == 1 else f"+{delta} {_plural_ratings(delta)}"
    lines = [
        f"⭐ <b>{head}</b> · {html.escape(product_label)} · {html.escape(store_label)}",
    ]
    stars = event.get("stars")
    if stars and len(stars) == 1:
        lines.append(f"{'⭐' * stars[0]} ({stars[0]}/5) · без текста")
    elif stars:
        lines.append("Оценки: " + ", ".join(f"{s}/5" for s in stars) + " · без текста")
    elif event.get("new_avg"):
        lines.append(f"В среднем {event['new_avg']}/5 · без текста")
    else:
        lines.append("Без текста")
    total = event.get("total")
    if total:
        lines.append("")
        lines.append(f"📊 Всего оценок: {total} · средняя {float(event.get('avg') or 0):.2f}")
    return "\n".join(lines)


def _collect_ratings(product: str, reviews: list[dict], state: dict) -> tuple[list[dict], dict]:
    """Снять счётчики оценок App Store + RuStore и вычислить события.

    Возвращает (events, обновлённый state продукта). Нестроенный/упавший стор
    мягко пропускается, его state не трогается.
    """
    prod_state = dict(state.get(product) or {})
    events: list[dict] = []

    try:
        app_id = asc._app_id_for(product)  # type: ignore[arg-type]
        counts = asc.fetch_rating_counts(app_id)
        if counts:
            rss = asc.fetch_reviews_by_country(app_id)
            ev, prod_state["app_store"] = compute_app_store_rating_events(
                prod_state.get("app_store"), counts, rss,
            )
            events.extend(ev)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"INFO: App Store ratings skipped for {product}: {exc!r}\n")

    try:
        if rustore._is_configured():
            pkg = rustore._package_for(product)  # type: ignore[arg-type]
            stats = rustore.fetch_rating_stats(rustore._cached_token(), pkg)
            if stats is not None:
                text_reviews = [r for r in reviews if r.get("store") == "rustore"]
                ev, prod_state["rustore"] = compute_rustore_rating_events(
                    prod_state.get("rustore"), stats, text_reviews,
                )
                events.extend(ev)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"INFO: RuStore ratings skipped for {product}: {exc!r}\n")

    return events, prod_state


# ===================================================================
# Collection — per product across three stores
# ===================================================================

def _collect_reviews(product: str) -> list[dict]:
    """Собрать per-review dict-списки по трём сторам для одного продукта.

    Каждый стор в своём try/except → один битый/нестроенный стор не валит
    продукт. Нестроенный стор (нет ASC_APP_ID_* / play|rustore не настроены) —
    мягко пропускается.
    """
    out: list[dict] = []

    # --- App Store (RSS, нужен только app_id) ---
    try:
        app_id = asc._app_id_for(product)  # type: ignore[arg-type]
        out.extend(asc.fetch_reviews_list(app_id))
    except Exception as exc:  # noqa: BLE001 — нет app_id / ошибка → skip
        sys.stderr.write(
            f"INFO: App Store reviews skipped for {product}: {exc!r}\n"
        )

    # --- Google Play (нужен Service Account + package) ---
    try:
        if play._is_configured():
            pkg = play._package_for(product)  # type: ignore[arg-type]
            creds = play._get_credentials()
            out.extend(play.fetch_reviews_list(creds, pkg))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"INFO: Google Play reviews skipped for {product}: {exc!r}\n"
        )

    # --- RuStore (нужен JWS-ключ + package) ---
    try:
        if rustore._is_configured():
            pkg = rustore._package_for(product)  # type: ignore[arg-type]
            bearer = rustore._cached_token()
            out.extend(rustore.fetch_reviews_list(bearer, pkg))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"INFO: RuStore reviews skipped for {product}: {exc!r}\n"
        )

    return out


# ===================================================================
# Main orchestration
# ===================================================================

def _send_safely(card: str, what: str) -> None:
    """Отправить карточку; любая ошибка → stderr, наружу не выходит."""
    try:
        if not send_card(card):
            sys.stderr.write(f"WARN: card not sent ({what})\n")
    except Exception as exc:  # noqa: BLE001 — одна не валит остальные
        sys.stderr.write(f"ERROR: card send raised ({what}): {exc!r}\n")


def main(
    seen_path: Path | None = None, ratings_path: Path | None = None,
) -> int:
    """Entry для workflow. Для каждого продукта/стора: собрать отзывы, вычислить
    новые, отправить по одной карточке, обновить seen. Baseline — без рассылки.
    Затем — оценки без текста (App Store + RuStore) по watermark-состоянию.
    """
    if seen_path is None:
        seen_path = SEEN_PATH
    if ratings_path is None:
        ratings_path = seen_path.parent / RATINGS_STATE_NAME

    seen = load_seen(seen_path)
    ratings_state = load_seen(ratings_path)
    summary: list[str] = []

    for product in PRODUCTS:
        all_reviews = _collect_reviews(product)
        for store in STORES:
            store_reviews = [r for r in all_reviews if r.get("store") == store]
            new, baseline = find_new(seen, product, store, store_reviews)
            sent = 0
            if not baseline:
                for r in new:
                    if not is_fresh(r):
                        continue  # старый «новый» — засеется молча ниже
                    _send_safely(
                        format_card(r, product),
                        f"product={product} store={store} id={r['review_id']}",
                    )
                    sent += 1
            summary.append(
                f"{product}/{store}: {len(store_reviews)} reviews, "
                f"{'baseline' if baseline else f'{sent} sent'}"
            )
            # Засеять/обновить seen ВСЕМИ отзывами этого прогона (новые+старые),
            # даже в baseline — это и есть baseline-засев.
            update_seen(seen, product, store, store_reviews)

        events, ratings_state[product] = _collect_ratings(
            product, all_reviews, ratings_state,
        )
        for ev in events:
            _send_safely(
                format_rating_card(ev, product),
                f"rating product={product} store={ev['store']}",
            )
        if events:
            summary.append(f"{product}: {len(events)} rating card(s)")

    save_seen(seen_path, seen)
    save_seen(ratings_path, ratings_state)
    # Сводка в лог — чтобы «тихий ноль» по стору было видно в GH Actions.
    print("\n".join(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
