"""Тесты сводки оценок для дашборда студии (quick 261111-rts). Без сети."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.review_notifier import ratings_summary as rs

AS_HTML = (
    'xx{"$kind":"Ratings","productId":"6784724566","ratingAverage":2.3,'
    '"totalNumberOfRatings":3,"context":"productPage","ratingCounts":[1,0,0,0,2]}],yy'
)
GP_HTML = (
    'AF_initDataCallback({key: \'ds:9\', data:[[[[]]],[null,null,[{"52":[["4,7",4.714286],'
    '[null,["0",0],["0",0],["1",1],["2",2],["11",11]],["15",15],["10",10],null,[null,[],[]]]}]'
)


def test_parse_app_store_page_order_5_to_1():
    assert rs.parse_app_store_page(AS_HTML) == {5: 1, 4: 0, 3: 0, 2: 0, 1: 2}


def test_parse_app_store_page_rejects_mismatch_and_garbage():
    bad = AS_HTML.replace('"totalNumberOfRatings":3', '"totalNumberOfRatings":4')
    assert rs.parse_app_store_page(bad) is None
    assert rs.parse_app_store_page("<html></html>") is None


def test_parse_google_play_page():
    hist, ratings, reviews = rs.parse_google_play_page(GP_HTML)
    assert hist == {1: 0, 2: 0, 3: 1, 4: 2, 5: 11}
    assert (ratings, reviews) == (15, 10)
    assert rs.parse_google_play_page("Оценок пока нет") is None


def test_stars_entry():
    e = rs._stars_entry({1: 2, 2: 0, 3: 0, 4: 0, 5: 1}, 1)
    assert e == {"avg": 2.33, "ratings": 3, "reviews": 1,
                 "stars": {"5": 1, "4": 0, "3": 0, "2": 0, "1": 2}}
    assert rs._stars_entry({s: 0 for s in range(1, 6)}, 0) is None


def test_merge_keeps_old_on_failure_drops_on_empty_and_bumps_only_on_change():
    old = {"avg": 5.0, "ratings": 1, "reviews": 0, "stars": {"5": 1, "4": 0, "3": 0, "2": 0, "1": 0}}
    prev = {"updated_at": "T0", "products": {"diktum": {
        "app_store": dict(old, updated_at="T0"),
        "google_play": dict(old, updated_at="T0"),
        "rustore": dict(old, updated_at="T0"),
    }}}
    # стор не ответил / то же значение → без изменений, updated_at прежний
    same = rs.merge(prev, {"diktum": {"app_store": None, "google_play": dict(old),
                                       "rustore": dict(old)}}, "T1")
    assert same == prev
    # новое значение → сдвиг; empty → стор убран
    new = dict(old, ratings=2, stars={"5": 2, "4": 0, "3": 0, "2": 0, "1": 0})
    out = rs.merge(prev, {"diktum": {"app_store": new, "google_play": {"empty": True},
                                      "rustore": None}}, "T1")
    assert out["updated_at"] == "T1"
    assert out["products"]["diktum"]["app_store"] == dict(new, updated_at="T1")
    assert "google_play" not in out["products"]["diktum"]
    assert out["products"]["diktum"]["rustore"]["updated_at"] == "T0"


def test_write_summary_never_raises_and_has_no_text(tmp_path: Path):
    path = tmp_path / "ratings_summary.json"
    seen = {"listvia": {"app_store": ["a", "b"]}}

    def fake_collect(product, store, seen_arg):
        if store == "app_store":
            return rs._stars_entry({1: 2, 2: 0, 3: 0, 4: 0, 5: 1}, len(seen_arg[product]["app_store"]))
        if store == "google_play":
            raise RuntimeError("GPLAY_PACKAGE_LISTVIA not set")
        return None

    with patch.object(rs, "_collect", side_effect=fake_collect):
        rs.write_summary(path, ["listvia"], seen)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["products"]["listvia"]) == {"app_store"}
    assert data["products"]["listvia"]["app_store"]["reviews"] == 2
    assert set(data["products"]["listvia"]["app_store"]) == {
        "avg", "ratings", "reviews", "stars", "updated_at"}


def test_collect_app_store_sums_countries():
    pages = {"ru": AS_HTML, "us": AS_HTML.replace("[1,0,0,0,2]", "[1,0,0,0,0]")
             .replace('"totalNumberOfRatings":3', '"totalNumberOfRatings":1')}

    def fake_get(url):
        for cc, html in pages.items():
            if f"/{cc}/app/" in url:
                return html
        return None

    with patch.object(rs.asc, "_app_id_for", return_value="1"), \
            patch.object(rs, "_get", side_effect=fake_get):
        e = rs.collect_app_store("listvia", {})
    assert e["ratings"] == 4 and e["stars"]["5"] == 2 and e["stars"]["1"] == 2
    assert e["reviews"] == 0
