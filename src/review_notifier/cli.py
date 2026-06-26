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

Мягкая деградация: отсутствие секретов / сети → всё мягко пропускается,
исключение наружу не выбрасывается.
"""
from __future__ import annotations

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
MAX_SEEN_PER_PAIR: Final[int] = 500
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

def main(seen_path: Path | None = None) -> int:
    """Entry для workflow. Для каждого продукта/стора: собрать отзывы, вычислить
    новые, отправить по одной карточке, обновить seen. Baseline — без рассылки.
    """
    if seen_path is None:
        seen_path = SEEN_PATH

    seen = load_seen(seen_path)

    for product in PRODUCTS:
        all_reviews = _collect_reviews(product)
        for store in STORES:
            store_reviews = [r for r in all_reviews if r.get("store") == store]
            new, baseline = find_new(seen, product, store, store_reviews)
            if not baseline:
                for r in new:
                    try:
                        ok = send_card(format_card(r, product))
                        if not ok:
                            sys.stderr.write(
                                f"WARN: card not sent (product={product} "
                                f"store={store} id={r['review_id']})\n"
                            )
                    except Exception as exc:  # noqa: BLE001 — одна не валит остальные
                        sys.stderr.write(
                            f"ERROR: card send raised (product={product} "
                            f"store={store} id={r['review_id']}): {exc!r}\n"
                        )
            # Засеять/обновить seen ВСЕМИ отзывами этого прогона (новые+старые),
            # даже в baseline — это и есть baseline-засев.
            update_seen(seen, product, store, store_reviews)

    save_seen(seen_path, seen)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
