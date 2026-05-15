"""Unit tests for src/store_metrics/asc.py — installs stub + iTunes RSS.

After 2026-05-15 canonical pivot (full-auto mode, drop manual CSV):
    - Installs path = STUB (always None + blocker error string).
      Apple Integrations API заблокирован cert recovery; когда починят
      и появятся ASC_KEY_ID/ASC_ISSUER_ID/ASC_PRIVATE_KEY — добавим JWT
      path и снимем stub.
    - Ratings path keeps iTunes Customer Reviews RSS (no auth, no blocker).
    - No more Reporter Token / Vendor Number / CSV reader — все ушли.

HTTP calls (RSS only now) are mocked via unittest.mock.patch on
src.store_metrics._http.fetch_with_retry.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import asc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store_metrics"

RSS_CENTRY = json.loads((FIXTURES / "apple_rss_centry_with_reviews.json").read_text())
RSS_DIKTUM_EMPTY = json.loads((FIXTURES / "apple_rss_diktum_empty.json").read_text())

APPLE_ID_CENTRY = "1000000000"
APPLE_ID_DIKTUM = "2000000000"

# Mon 2026-05-11 == ISO 2026-W20.
WEEK_W20 = dt.date(2026, 5, 11)


# ===================================================================
# env / configuration
# ===================================================================

def _set_envs(monkeypatch, *, all_present: bool = True) -> None:
    if all_present:
        monkeypatch.setenv("ASC_APP_ID_CENTRY", APPLE_ID_CENTRY)
        monkeypatch.setenv("ASC_APP_ID_DIKTUM", APPLE_ID_DIKTUM)
    else:
        for k in ("ASC_APP_ID_CENTRY", "ASC_APP_ID_DIKTUM"):
            monkeypatch.delenv(k, raising=False)


def test_is_configured_all_envs_set(monkeypatch):
    _set_envs(monkeypatch, all_present=True)
    assert asc._is_configured() is True


def test_is_configured_missing_envs(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    assert asc._is_configured() is False


def test_is_configured_partial_envs_returns_false(monkeypatch):
    """Only one of two app IDs set → still False."""
    _set_envs(monkeypatch, all_present=False)
    monkeypatch.setenv("ASC_APP_ID_CENTRY", APPLE_ID_CENTRY)
    assert asc._is_configured() is False


def test_is_configured_empty_string_counts_as_missing(monkeypatch):
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.setenv("ASC_APP_ID_CENTRY", "")
    assert asc._is_configured() is False


def test_app_id_for_centry_and_diktum(monkeypatch):
    _set_envs(monkeypatch, all_present=True)
    assert asc._app_id_for("centry") == APPLE_ID_CENTRY
    assert asc._app_id_for("diktum") == APPLE_ID_DIKTUM


def test_app_id_for_missing_env_raises(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    with pytest.raises(RuntimeError, match="ASC_APP_ID_CENTRY"):
        asc._app_id_for("centry")


def test_app_id_for_strips_whitespace(monkeypatch):
    """GH Secret storage may add trailing newline — _app_id_for must strip."""
    monkeypatch.setenv("ASC_APP_ID_CENTRY", "1000000000\n")
    monkeypatch.setenv("ASC_APP_ID_DIKTUM", "  2000000000  ")
    assert asc._app_id_for("centry") == "1000000000"
    assert asc._app_id_for("diktum") == "2000000000"


# ===================================================================
# Installs blocker constant — sanity на shape сообщения
# ===================================================================

def test_installs_blocker_error_mentions_apple_integrations():
    """Stable error string должен содержать ключевые маркеры для digest."""
    msg = asc._INSTALLS_BLOCKER_ERROR
    assert "Apple Integrations" in msg
    assert "cert recovery" in msg
    # Указание на 3 будущих secret name — для удобства когда юзер откроет error.
    assert "ASC_KEY_ID" in msg
    assert "ASC_ISSUER_ID" in msg
    assert "ASC_PRIVATE_KEY" in msg


# ===================================================================
# _fetch_rss_ratings
# ===================================================================

def test_fetch_rss_ratings_aggregates_across_countries():
    """Same fixture for RU/US → 3 entries each, 5,4,5 → avg=28/6."""
    def fake_fetch(url: str, method: str = "GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru", "us"],
        )
    assert count == 6
    assert avg == pytest.approx(28 / 6)


def test_fetch_rss_ratings_empty_feed_returns_none():
    """Diktum at launch — feed has no 'entry' key → (None, 0)."""
    def fake_fetch(url: str, method: str = "GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(APPLE_ID_DIKTUM, countries=["ru"])
    assert avg is None
    assert count == 0


def test_fetch_rss_ratings_handles_single_entry_dict():
    """RSS sometimes returns entry as dict (not list) when only 1 review."""
    single_entry = {
        "feed": {
            "entry": {"im:rating": {"label": "4"}}
        }
    }

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = single_entry
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru"],
        )
    assert avg == 4.0
    assert count == 1


def test_fetch_rss_ratings_skips_country_on_http_error():
    """One country returns 500, second OK → result includes second only."""
    def fake_fetch(url: str, method: str = "GET", **kwargs):
        m = MagicMock()
        if "/ru/" in url:
            m.status_code = 500
            m.content = b""
            return m
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru", "us"],
        )
    assert count == 3
    assert avg == pytest.approx(14 / 3)


def test_fetch_rss_ratings_skips_invalid_rating_labels():
    """Garbled label → skip entry, not crash."""
    bad_payload = {
        "feed": {
            "entry": [
                {"im:rating": {"label": "abc"}},
                {"im:rating": {"label": "5"}},
                {"im:rating": {"label": "9"}},
                {"im:rating": "not-a-dict"},
                {},
            ]
        }
    }

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = bad_payload
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru"],
        )
    assert count == 1
    assert avg == 5.0


def test_fetch_rss_ratings_tolerates_network_exception_per_country():
    """ConnectionError on one country → caught, processing continues."""
    def fake_fetch(url, method="GET", **kwargs):
        if "/ru/" in url:
            raise RuntimeError("network down")
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru", "us"],
        )
    assert count == 3
    assert avg == pytest.approx(14 / 3)


def test_fetch_rss_ratings_default_countries_includes_ru_us_kz_by_ua():
    """Smoke: with no countries= arg, default list includes 5 markets."""
    calls: list[str] = []

    def fake_fetch(url, method="GET", **kwargs):
        calls.append(url)
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        asc._fetch_rss_ratings(APPLE_ID_CENTRY)
    assert len(calls) == 5
    for cc in ("ru", "us", "kz", "by", "ua"):
        assert any(f"/{cc}/" in u for u in calls)


def test_fetch_rss_ratings_non_json_response_skipped():
    """Bad JSON → skip country, no exception."""
    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.side_effect = ValueError("bad json")
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru"],
        )
    assert avg is None
    assert count == 0


# ===================================================================
# fetch_weekly — integration (stub mode)
# ===================================================================

def test_fetch_weekly_unconfigured_returns_mock(monkeypatch):
    """Without envs → mock StoreSnapshot, no HTTP / stub calls."""
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_weekly("centry", WEEK_W20)
    assert snap.product == "centry"
    assert snap.store == "app_store"
    assert snap.installs == 23
    assert snap.rating == 4.7
    assert snap.top_country == "RU"


def test_fetch_weekly_installs_returns_none_with_blocker_message(monkeypatch):
    """Configured → installs=None + error mentions Apple Integrations blocker.

    Это canonical state до Apple cert recovery — installs физически нельзя
    получить, RSS rating работает независимо.
    """
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    # Installs всегда None — Integrations API заблокирован cert recovery.
    assert snap.installs is None
    # Error string должен явно говорить про блокер.
    assert snap.error is not None
    assert "Apple Integrations" in snap.error
    assert "cert recovery" in snap.error
    # Rating всё равно подхватывается RSS — независимая axis.
    assert snap.rating is not None
    # top_country остаётся None — нет installs данных для группировки.
    assert snap.top_country is None
    assert snap.top_country_share is None


def test_fetch_weekly_rss_fails_installs_still_blocker(monkeypatch):
    """RSS network failure → rating=None, installs всё равно None с blocker.

    Failure RSS не должно менять message — installs blocker остаётся
    primary error, rating падает без отдельной записи (мы лишь warn в stderr).
    """
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        raise RuntimeError("network down")

    with patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.rating is None
    # Error всё ещё про Integrations blocker (RSS failure swallowed).
    assert snap.error is not None
    assert "Apple Integrations" in snap.error


def test_fetch_weekly_for_diktum_isolates_correctly(monkeypatch):
    """Same envs, requesting Diktum → snapshot built for diktum app_id."""
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("diktum", WEEK_W20)

    assert snap.product == "diktum"
    assert snap.store == "app_store"
    assert snap.installs is None
    assert snap.rating is None  # empty RSS fixture for Diktum
    assert snap.error is not None
    assert "Apple Integrations" in snap.error


# ===================================================================
# fetch_previous
# ===================================================================

def test_fetch_previous_unconfigured_returns_mock(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_previous("diktum", WEEK_W20)
    assert snap.installs == 22   # _MOCK_PREV
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_previous_shifts_week_by_7_days(monkeypatch):
    """Configured → fetch_weekly called with week_start - 7 days."""
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_previous("centry", WEEK_W20)

    assert snap.week_start == dt.date(2026, 5, 4)
    assert snap.installs is None
    assert snap.error is not None
    assert "Apple Integrations" in snap.error
