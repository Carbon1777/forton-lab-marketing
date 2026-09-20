"""Unit tests for the review_notifier feature (quick 260626-ozg).

Two layers under test:

  Task 1 — per-review list fetchers in the three store adapters:
      asc.fetch_reviews_list(app_id)            → list[dict]
      play.fetch_reviews_list(credentials, pkg) → list[dict]
      rustore.fetch_reviews_list(bearer, pkg)   → list[dict]
    Each returns a unified per-review dict schema:
      {review_id:str, store:str, rating:int, author:str, text:str, date:str|None}

  Task 2 — review_notifier.cli orchestration:
      load_seen / save_seen / find_new / update_seen / format_card /
      send_card / _collect_reviews / main
    dedup + baseline (first run seeds without sending) + HTML escape +
    soft-skip of unconfigured stores + prune-without-resend + per-card
    failure isolation.

HTTP / TG / google calls are mocked via unittest.mock — no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import asc, play, rustore


# ===================================================================
# Helpers
# ===================================================================

def _mock_response(json_body: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    return resp


# Apple RSS fixture: first entry = app metadata (NO im:rating) → must be skipped.
# Two real reviews follow. `content` may be a list (multi type nodes) like real RSS.
_RSS_RU = {
    "feed": {
        "entry": [
            {
                # app metadata entry — no im:rating, must be skipped
                "id": {"label": "https://itunes.apple.com/app/id123"},
                "im:name": {"label": "Centry"},
                "title": {"label": "Centry"},
            },
            {
                "id": {"label": "review-ru-1"},
                "author": {"name": {"label": "Иван"}},
                "im:rating": {"label": "5"},
                "title": {"label": "Отлично"},
                "content": [{"label": "Хорошее приложение", "attributes": {"type": "text"}}],
                "updated": {"label": "2026-06-20T08:11:00-07:00"},
            },
            {
                "id": {"label": "review-ru-2"},
                "author": {"name": {"label": "Пётр"}},
                "im:rating": {"label": "4"},
                "title": {"label": "Норм"},
                "content": {"label": "Неплохо"},  # single dict form
                "updated": {"label": "2026-06-19T09:45:00-07:00"},
            },
        ]
    }
}

# US feed re-returns review-ru-1 (cross-country dup) plus a unique one.
_RSS_US = {
    "feed": {
        "entry": [
            {
                "id": {"label": "review-ru-1"},  # duplicate across countries
                "author": {"name": {"label": "Ivan"}},
                "im:rating": {"label": "5"},
                "title": {"label": "Great"},
                "content": [{"label": "Nice app"}],
                "updated": {"label": "2026-06-20T08:11:00-07:00"},
            },
            {
                "id": {"label": "review-us-1"},
                "author": {"name": {"label": "John"}},
                "im:rating": {"label": "3"},
                "title": {"label": "Meh"},
                "content": [{"label": "okay"}],
                "updated": None,  # date may be absent
            },
        ]
    }
}

_RSS_EMPTY = {"feed": {"updated": {"label": "x"}}}  # no entry key


# ===================================================================
# Task 1 — asc.fetch_reviews_list
# ===================================================================

def test_asc_fetch_reviews_list_extracts_per_review_dicts():
    def fake_fetch(url, method="GET", **kw):
        if "/ru/" in url:
            return _mock_response(_RSS_RU)
        return _mock_response(_RSS_EMPTY)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        reviews = asc.fetch_reviews_list("123")

    by_id = {r["review_id"]: r for r in reviews}
    # metadata entry skipped, 2 reviews extracted from RU
    assert set(by_id) == {"review-ru-1", "review-ru-2"}
    r1 = by_id["review-ru-1"]
    assert r1["store"] == "app_store"
    assert r1["rating"] == 5
    assert r1["author"] == "Иван"
    # title + body composed into text (Apple review title is meaningful)
    assert "Хорошее приложение" in r1["text"]
    assert "Отлично" in r1["text"]
    assert r1["date"] == "2026-06-20T08:11:00-07:00"
    r2 = by_id["review-ru-2"]
    assert "Неплохо" in r2["text"]  # single-dict content form handled


def test_asc_fetch_reviews_list_dedup_across_countries():
    def fake_fetch(url, method="GET", **kw):
        if "/ru/" in url:
            return _mock_response(_RSS_RU)
        if "/us/" in url:
            return _mock_response(_RSS_US)
        return _mock_response(_RSS_EMPTY)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        reviews = asc.fetch_reviews_list("123")

    ids = [r["review_id"] for r in reviews]
    # review-ru-1 appears in both RU and US feeds → collapsed once
    assert ids.count("review-ru-1") == 1
    assert "review-us-1" in ids
    us = next(r for r in reviews if r["review_id"] == "review-us-1")
    assert us["date"] is None  # absent updated → None


def test_asc_fetch_reviews_list_country_error_does_not_raise():
    def fake_fetch(url, method="GET", **kw):
        if "/ru/" in url:
            raise ConnectionError("boom")
        return _mock_response(_RSS_EMPTY)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        reviews = asc.fetch_reviews_list("123")
    assert reviews == []


# ===================================================================
# Task 1 — play.fetch_reviews_list
# ===================================================================

def _play_resp(reviews, next_token=None):
    body: dict = {"reviews": reviews}
    if next_token:
        body["tokenPagination"] = {"nextPageToken": next_token}
    return body


def test_play_fetch_reviews_list_extracts_and_paginates():
    page0 = _play_resp(
        [
            {
                "reviewId": "gp-1",
                "authorName": "Анна",
                "comments": [{
                    "userComment": {
                        "text": "Класс",
                        "starRating": 5,
                        "lastModified": {"seconds": "1718870400"},
                    }
                }],
            }
        ],
        next_token="tok2",
    )
    page1 = _play_resp(
        [
            {
                "reviewId": "gp-2",
                "authorName": "Олег",
                "comments": [{
                    "userComment": {"text": "Норм", "starRating": 4, "lastModified": {}},
                }],
            },
            {
                # no starRating → skip
                "reviewId": "gp-3",
                "authorName": "X",
                "comments": [{"userComment": {"text": "no stars"}}],
            },
        ]
    )

    service = MagicMock()
    list_mock = service.reviews.return_value.list
    list_mock.return_value.execute.side_effect = [page0, page1]

    with patch("googleapiclient.discovery.build", return_value=service):
        reviews = play.fetch_reviews_list(MagicMock(), "pkg")

    by_id = {r["review_id"]: r for r in reviews}
    assert set(by_id) == {"gp-1", "gp-2"}  # gp-3 skipped (no starRating)
    assert by_id["gp-1"]["store"] == "google_play"
    assert by_id["gp-1"]["rating"] == 5
    assert by_id["gp-1"]["author"] == "Анна"
    assert by_id["gp-1"]["text"] == "Класс"
    assert by_id["gp-1"]["date"] == "2024-06-20"  # ISO date from epoch seconds
    assert by_id["gp-2"]["date"] is None  # no seconds


def test_play_fetch_reviews_list_never_raises():
    service = MagicMock()
    service.reviews.return_value.list.return_value.execute.side_effect = RuntimeError("x")
    with patch("googleapiclient.discovery.build", return_value=service):
        reviews = play.fetch_reviews_list(MagicMock(), "pkg")
    assert reviews == []


# ===================================================================
# Task 1 — rustore.fetch_reviews_list
# ===================================================================

def _rustore_page(content, last=True):
    return {"code": "OK", "body": {"content": content, "last": last}}


def test_rustore_fetch_reviews_list_extracts_published_only():
    page = _rustore_page([
        {
            "commentId": 1001,
            "userName": "Иван",
            "appRating": 5,
            "commentText": "Хорошее приложение",
            "commentStatus": "PUBLISHED",
            "commentDate": "2026-06-12T08:11:00+03:00",
        },
        {
            "commentId": 1002,
            "userName": "Скрытый",
            "appRating": 1,
            "commentText": "спам",
            "commentStatus": "HIDDEN",  # filtered out
            "commentDate": "2026-06-13T00:00:00+03:00",
        },
    ])
    with patch.object(rustore._http, "fetch_with_retry", return_value=_mock_response(page)):
        reviews = rustore.fetch_reviews_list("bearer", "pkg")

    assert len(reviews) == 1
    r = reviews[0]
    assert r["review_id"] == "1001"
    assert r["store"] == "rustore"
    assert r["rating"] == 5
    assert r["author"] == "Иван"
    assert r["text"] == "Хорошее приложение"
    assert r["date"] == "2026-06-12T08:11:00+03:00"


def test_rustore_fetch_reviews_list_page_error_breaks_gracefully():
    with patch.object(
        rustore._http, "fetch_with_retry",
        return_value=_mock_response({"err": "x"}, status=500),
    ):
        reviews = rustore.fetch_reviews_list("bearer", "pkg")
    assert reviews == []


# ===================================================================
# Task 2 — review_notifier.cli
# ===================================================================

from src.review_notifier import cli as rn  # noqa: E402


def _review(rid, store="app_store", rating=5, author="A", text="t", date=None):
    return {
        "review_id": rid, "store": store, "rating": rating,
        "author": author, "text": text, "date": date,
    }


def test_seen_roundtrip(tmp_path):
    p = tmp_path / "reviews_seen.json"
    assert rn.load_seen(p) == {}
    data = {"centry": {"app_store": ["a", "b"]}}
    rn.save_seen(p, data)
    assert rn.load_seen(p) == data


def test_find_new_baseline_first_run():
    seen = {}
    new, baseline = rn.find_new(seen, "centry", "app_store",
                                [_review("a"), _review("b")])
    assert baseline is True
    assert new == []


def test_find_new_only_unseen():
    seen = {"centry": {"app_store": ["a"]}}
    new, baseline = rn.find_new(seen, "centry", "app_store",
                                [_review("a"), _review("b")])
    assert baseline is False
    assert [r["review_id"] for r in new] == ["b"]


def test_find_new_empty_seen_list_is_not_baseline():
    # store seen before (empty list) → not baseline, everything new
    seen = {"centry": {"app_store": []}}
    new, baseline = rn.find_new(seen, "centry", "app_store", [_review("a")])
    assert baseline is False
    assert [r["review_id"] for r in new] == ["a"]


def test_update_seen_creates_keys_and_keeps_order():
    seen = {}
    rn.update_seen(seen, "centry", "app_store", [_review("a"), _review("b")])
    assert seen["centry"]["app_store"] == ["a", "b"]
    rn.update_seen(seen, "centry", "app_store", [_review("b"), _review("c")])
    assert seen["centry"]["app_store"] == ["a", "b", "c"]  # no dup, append new


def test_update_seen_prune_keeps_recent_no_resend():
    seen = {}
    ids = [_review(str(i)) for i in range(rn.MAX_SEEN_PER_PAIR + 50)]
    rn.update_seen(seen, "centry", "app_store", ids)
    kept = seen["centry"]["app_store"]
    assert len(kept) == rn.MAX_SEEN_PER_PAIR
    # newest IDs retained (tail), oldest dropped from head
    assert kept[-1] == str(rn.MAX_SEEN_PER_PAIR + 49)
    # a recently-seen id must NOT count as new on the next run (no resend)
    new, baseline = rn.find_new(seen, "centry", "app_store",
                                [_review(str(rn.MAX_SEEN_PER_PAIR + 49))])
    assert baseline is False
    assert new == []


def test_format_card_escapes_html_and_renders_stars():
    review = _review("x", rating=3, author="<b>hax</b>", text="a < b & c")
    card = rn.format_card(review, "centry")
    assert "&lt;b&gt;hax&lt;/b&gt;" in card
    assert "a &lt; b &amp; c" in card
    assert card.count("⭐") == 3


def test_send_card_no_creds_returns_false(monkeypatch):
    monkeypatch.delenv("TG_PLANNER_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_OWNER_CHAT_ID", raising=False)
    assert rn.send_card("hi") is False


def test_collect_reviews_soft_skip_unconfigured(monkeypatch):
    # No ASC_APP_ID_*, no play/rustore config → all stores skipped, no crash.
    for k in list(__import__("os").environ):
        if k.startswith(("ASC_APP_ID", "GPLAY_", "GOOGLE_PLAY", "RUSTORE_")):
            monkeypatch.delenv(k, raising=False)
    reviews = rn._collect_reviews("centry")
    assert reviews == []


def test_main_baseline_seeds_without_sending(monkeypatch, tmp_path):
    p = tmp_path / "seen.json"
    monkeypatch.setattr(
        rn, "_collect_reviews",
        lambda product: [_review("a", text="hi"), _review("b", text="yo")]
        if product == "centry" else [],
    )
    sent = []
    monkeypatch.setattr(rn, "send_card", lambda card: sent.append(card) or True)

    rc = rn.main(seen_path=p)
    assert rc == 0
    assert sent == []  # baseline → nothing sent
    seen = rn.load_seen(p)
    assert set(seen["centry"]["app_store"]) == {"a", "b"}


def test_main_sends_only_new(monkeypatch, tmp_path):
    p = tmp_path / "seen.json"
    rn.save_seen(p, {"centry": {"app_store": ["a"]}})
    monkeypatch.setattr(
        rn, "_collect_reviews",
        lambda product: [_review("a", text="old"), _review("b", text="new")]
        if product == "centry" else [],
    )
    sent = []
    monkeypatch.setattr(rn, "send_card", lambda card: sent.append(card) or True)

    rn.main(seen_path=p)
    assert len(sent) == 1
    assert "new" in sent[0]
    seen = rn.load_seen(p)
    assert set(seen["centry"]["app_store"]) == {"a", "b"}


def test_main_one_send_failure_continues(monkeypatch, tmp_path):
    p = tmp_path / "seen.json"
    rn.save_seen(p, {"centry": {"app_store": ["seed"]}})
    monkeypatch.setattr(
        rn, "_collect_reviews",
        lambda product: [_review("n1", text="one"), _review("n2", text="two")]
        if product == "centry" else [],
    )
    calls = []

    def flaky(card):
        calls.append(card)
        if "one" in card:
            raise RuntimeError("tg down")
        return True

    monkeypatch.setattr(rn, "send_card", flaky)
    rc = rn.main(seen_path=p)  # must not raise
    assert rc == 0
    assert len(calls) == 2  # both attempted despite first raising
    seen = rn.load_seen(p)
    assert set(seen["centry"]["app_store"]) == {"seed", "n1", "n2"}


# ===================================================================
# quick 260917-g60 — RuStore real payload, ratings without text, freshness
# ===================================================================

import datetime as _dt  # noqa: E402

# Реальная форма /comment (verified 2026-09-17): body — СПИСОК, без пагинации.
_RUSTORE_REAL_LIST = {
    "code": "OK", "message": "OK",
    "body": [
        {
            "packageName": "pkg", "appId": 1, "commentId": 2001, "userName": "Ольга",
            "appRating": 4, "commentStatus": "PUBLISHED", "feedbackType": "COMMENT",
            "commentDate": "2026-09-16 10:00:00.000", "commentText": "Норм",
            "commentDateIso": "2026-09-16T10:00:00.000Z", "devResponses": [],
        },
    ],
}


def test_rustore_fetch_reviews_list_real_list_body():
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=_mock_response(_RUSTORE_REAL_LIST),
    ) as m:
        reviews = rustore.fetch_reviews_list("bearer", "pkg")
    assert [r["review_id"] for r in reviews] == ["2001"]
    assert reviews[0]["rating"] == 4
    assert reviews[0]["date"] == "2026-09-16T10:00:00.000Z"
    assert m.call_count == 1  # неполная страница = последняя


def test_rustore_weekly_fetch_reviews_real_list_body():
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=_mock_response(_RUSTORE_REAL_LIST),
    ):
        assert rustore._fetch_reviews("bearer", "pkg") == (4.0, 1)


def test_rustore_fetch_rating_stats_parses_statistic():
    payload = {"code": "OK", "body": {
        "ratings": {"amountFive": 3, "amountFour": 1, "amountThree": 0,
                    "amountTwo": 0, "amountOne": 0},
        "averageUserRating": 4.75, "totalRatings": 4, "totalResponses": 1,
        "ratingsNoComments": 3,
    }}
    with patch.object(rustore._http, "fetch_with_retry", return_value=_mock_response(payload)) as m:
        stats = rustore.fetch_rating_stats("bearer", "pkg")
    assert m.call_args.kwargs["method"] == "GET"
    assert stats == {"per_star": {1: 0, 2: 0, 3: 0, 4: 1, 5: 3},
                     "total": 4, "no_comments": 3, "avg": 4.75}


def test_rustore_fetch_rating_stats_http_error_returns_none():
    with patch.object(rustore._http, "fetch_with_retry", return_value=_mock_response({}, status=403)):
        assert rustore.fetch_rating_stats("bearer", "pkg") is None


def test_asc_fetch_rating_counts_skips_missing_countries():
    def fake(url, method="GET", **kw):
        if "country=ru" in url:
            return _mock_response({"results": [{"userRatingCount": 8, "averageUserRating": 4.75}]})
        return _mock_response({"results": []})

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake):
        assert asc.fetch_rating_counts("1") == {"ru": {"count": 8, "avg": 4.75}}


def test_is_fresh_filters_old_reviews():
    now = _dt.datetime(2026, 9, 17, tzinfo=_dt.timezone.utc)
    assert rn.is_fresh({"date": "2026-09-16"}, now)
    assert rn.is_fresh({"date": "2026-09-10T01:25:59-07:00"}, now)
    assert not rn.is_fresh({"date": "2026-05-18T19:52:03.945Z"}, now)
    assert not rn.is_fresh({"date": "2026-04-23 14:09:51.238"}, now)
    assert rn.is_fresh({"date": None}, now)
    assert rn.is_fresh({"date": "мусор"}, now)


def test_main_old_new_review_seeded_silently(monkeypatch, tmp_path):
    p = tmp_path / "seen.json"
    rn.save_seen(p, {"centry": {"rustore": []}})
    monkeypatch.setattr(
        rn, "_collect_reviews",
        lambda product: [_review("old", store="rustore", date="2026-04-23T14:09:51Z")]
        if product == "centry" else [],
    )
    monkeypatch.setattr(rn, "_collect_ratings", lambda product, reviews, state: ([], {}))
    sent = []
    monkeypatch.setattr(rn, "send_card", lambda card: sent.append(card) or True)
    rn.main(seen_path=p)
    assert sent == []
    assert rn.load_seen(p)["centry"]["rustore"] == ["old"]


def _rss(cc, *stars):
    return [_review(f"{cc}{i}", rating=s) for i, s in enumerate(stars)]


def test_app_store_ratings_baseline_then_single_star():
    counts = {"ru": {"count": 3, "avg": 5.0}}
    events, state = rn.compute_app_store_rating_events(None, counts, {"ru": _rss("ru", 5)})
    assert events == []
    assert state == {"ru": {"no_text": 2, "no_text_sum": 10, "count": 3, "sum": 15}}

    counts = {"ru": {"count": 4, "avg": 4.5}}  # +1 оценка 3★ без текста
    events, state = rn.compute_app_store_rating_events(state, counts, {"ru": _rss("ru", 5)})
    assert len(events) == 1
    assert events[0]["stars"] == [3]
    assert events[0]["country"] == "ru"
    assert state["ru"] == {"no_text": 3, "no_text_sum": 13, "count": 4, "sum": 18}


def test_app_store_ratings_old_state_schema_reseeds_without_event():
    # Старая схема (без "count") — миграция = baseline, карточка не шлётся.
    state = {"ru": {"no_text": 9, "no_text_sum": 43}}
    events, state2 = rn.compute_app_store_rating_events(
        state, {"ru": {"count": 9, "avg": 4.78}}, {"ru": _rss("ru", 5, 5, 5)},
    )
    assert events == []
    assert state2["ru"] == {"no_text": 6, "no_text_sum": 28, "count": 9, "sum": 43}


def test_app_store_ratings_empty_rss_feed_is_not_an_event():
    # Инцидент 2026-09-20: лента RSS отдала 0 отзывов при 9 живых оценках →
    # «без текста» подскочило с 6 до 9, но общее число оценок не менялось.
    state = {"ru": {"no_text": 6, "no_text_sum": 28, "count": 9, "sum": 43}}
    events, state2 = rn.compute_app_store_rating_events(
        state, {"ru": {"count": 9, "avg": 4.78}}, {"ru": []},
    )
    assert events == []
    assert state2["ru"]["no_text"] == 6  # watermark не задрался
    # RSS вернулась в норму + пришла настоящая оценка без текста 5★.
    events, state3 = rn.compute_app_store_rating_events(
        state2, {"ru": {"count": 10, "avg": 4.8}}, {"ru": _rss("ru", 5, 5, 5)},
    )
    assert len(events) == 1 and events[0]["delta"] == 1 and events[0]["stars"] == [5]
    assert state3["ru"] == {"no_text": 7, "no_text_sum": 33, "count": 10, "sum": 48}


def test_app_store_ratings_lookup_ahead_of_rss_is_not_an_event():
    # Обратный лаг: lookup уже посчитал новый ТЕКСТОВЫЙ отзыв, RSS ещё нет.
    state = {"ru": {"no_text": 2, "no_text_sum": 10, "count": 3, "sum": 15}}
    events, state2 = rn.compute_app_store_rating_events(
        state, {"ru": {"count": 4, "avg": 5.0}}, {"ru": _rss("ru", 5)},
    )
    assert len(events) == 1  # пока выглядит как оценка без текста
    # …а когда RSS догнала — повторной карточки нет.
    events, _ = rn.compute_app_store_rating_events(
        state2, {"ru": {"count": 4, "avg": 5.0}}, {"ru": _rss("ru", 5, 5)},
    )
    assert events == []


def test_app_store_ratings_rss_ahead_of_lookup_no_false_event():
    state = {"ru": {"no_text": 2, "no_text_sum": 10, "count": 3, "sum": 15}}
    # RSS уже показал новый текстовый отзыв, lookup ещё нет → no_text падает.
    events, state2 = rn.compute_app_store_rating_events(
        state, {"ru": {"count": 3, "avg": 5.0}}, {"ru": _rss("ru", 5, 4)},
    )
    assert events == []
    assert state2["ru"]["no_text"] == 2
    # Lookup догнал — число оценок без текста вернулось к watermark → тишина.
    events, _ = rn.compute_app_store_rating_events(
        state2, {"ru": {"count": 4, "avg": 4.75}}, {"ru": _rss("ru", 5, 4)},
    )
    assert events == []


def test_app_store_ratings_skip_country_when_rss_failed():
    state = {"ru": {"no_text": 0, "no_text_sum": 0, "count": 0, "sum": 0}}
    events, state2 = rn.compute_app_store_rating_events(
        state, {"ru": {"count": 5, "avg": 5.0}}, {"ru": None},
    )
    assert events == [] and state2 == state


def test_rustore_ratings_events():
    stats0 = {"per_star": {1: 0, 2: 0, 3: 0, 4: 0, 5: 3}, "total": 3, "no_comments": 2, "avg": 5.0}
    text = [_review("r1", store="rustore", rating=5)]
    events, state = rn.compute_rustore_rating_events(None, stats0, text)
    assert events == []
    assert state["no_comments"] == 2 and state["per_star_no_text"]["5"] == 2

    stats1 = {"per_star": {1: 1, 2: 0, 3: 0, 4: 0, 5: 3}, "total": 4, "no_comments": 3, "avg": 4.0}
    events, state = rn.compute_rustore_rating_events(state, stats1, text)
    assert len(events) == 1 and events[0]["stars"] == [1] and events[0]["delta"] == 1

    # Повтор без изменений — тишина.
    events, _ = rn.compute_rustore_rating_events(state, stats1, text)
    assert events == []


def test_format_rating_card():
    card = rn.format_rating_card(
        {"store": "app_store", "country": "ru", "delta": 1, "stars": [4],
         "new_avg": None, "total": 9, "avg": 4.67}, "listvia",
    )
    assert "Новая оценка" in card and "Листвия" in card and "App Store (RU)" in card
    assert "⭐⭐⭐⭐ (4/5) · без текста" in card
    assert "Всего оценок: 9" in card
    multi = rn.format_rating_card(
        {"store": "rustore", "country": None, "delta": 2, "stars": None,
         "new_avg": None, "total": 5, "avg": 5.0}, "diktum",
    )
    assert "+2 новые оценки" in multi and "RuStore" in multi


def test_main_sends_rating_cards_and_saves_state(monkeypatch, tmp_path):
    p = tmp_path / "seen.json"
    monkeypatch.setattr(rn, "_collect_reviews", lambda product: [])
    ev = {"store": "rustore", "country": None, "delta": 1, "stars": [5],
          "new_avg": None, "total": 4, "avg": 5.0}
    monkeypatch.setattr(
        rn, "_collect_ratings",
        lambda product, reviews, state: (([ev], {"rustore": {"no_comments": 3}})
                                         if product == "diktum" else ([], {})),
    )
    sent = []
    monkeypatch.setattr(rn, "send_card", lambda card: sent.append(card) or True)
    rn.main(seen_path=p)
    assert len(sent) == 1 and "Diktum" in sent[0]
    assert rn.load_seen(tmp_path / "ratings_seen.json")["diktum"] == {"rustore": {"no_comments": 3}}
