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
