"""Unit tests for src/store_metrics/asc.py — Apple Reporter + iTunes RSS.

All HTTP calls are mocked via unittest.mock.patch on src.store_metrics._http.
fetch_with_retry. Fixtures live in tests/fixtures/store_metrics/.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import asc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store_metrics"

SAMPLE_TSV = (FIXTURES / "apple_sales_weekly_sample.tsv").read_bytes()
EMPTY_TSV = (FIXTURES / "apple_sales_weekly_empty.tsv").read_bytes()
RSS_CENTRY = json.loads((FIXTURES / "apple_rss_centry_with_reviews.json").read_text())
RSS_DIKTUM_EMPTY = json.loads((FIXTURES / "apple_rss_diktum_empty.json").read_text())

APPLE_ID_CENTRY = "1000000000"
APPLE_ID_DIKTUM = "2000000000"


# ===================================================================
# env / configuration
# ===================================================================

def _set_envs(monkeypatch, *, all_present: bool = True) -> None:
    if all_present:
        monkeypatch.setenv("ASC_REPORTER_ACCESS_TOKEN", "tok-uuid")
        monkeypatch.setenv("ASC_VENDOR_NUMBER", "94183271")
        monkeypatch.setenv("ASC_APP_ID_CENTRY", APPLE_ID_CENTRY)
        monkeypatch.setenv("ASC_APP_ID_DIKTUM", APPLE_ID_DIKTUM)
    else:
        for k in (
            "ASC_REPORTER_ACCESS_TOKEN",
            "ASC_VENDOR_NUMBER",
            "ASC_APP_ID_CENTRY",
            "ASC_APP_ID_DIKTUM",
        ):
            monkeypatch.delenv(k, raising=False)


def test_is_configured_all_envs_set(monkeypatch):
    _set_envs(monkeypatch, all_present=True)
    assert asc._is_configured() is True


def test_is_configured_missing_envs(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    # set only 2 — still incomplete
    monkeypatch.setenv("ASC_REPORTER_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ASC_VENDOR_NUMBER", "94183271")
    assert asc._is_configured() is False


def test_is_configured_empty_string_counts_as_missing(monkeypatch):
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.setenv("ASC_REPORTER_ACCESS_TOKEN", "")
    assert asc._is_configured() is False


def test_app_id_for_centry_and_diktum(monkeypatch):
    _set_envs(monkeypatch, all_present=True)
    assert asc._app_id_for("centry") == APPLE_ID_CENTRY
    assert asc._app_id_for("diktum") == APPLE_ID_DIKTUM


def test_app_id_for_missing_env_raises(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    with pytest.raises(RuntimeError, match="ASC_APP_ID_CENTRY"):
        asc._app_id_for("centry")


# ===================================================================
# _target_sunday
# ===================================================================

def test_target_sunday_for_monday_week_start():
    """week_start = Mon 2026-05-11 → Sun 2026-05-17."""
    assert asc._target_sunday(dt.date(2026, 5, 11)) == dt.date(2026, 5, 17)


def test_target_sunday_for_iso_monday_returns_same_week_sunday():
    """ISO week W20 of 2026 — Mon May 11, Sun May 17."""
    week_start = dt.date(2026, 5, 11)
    sunday = asc._target_sunday(week_start)
    assert sunday.weekday() == 6   # 6 == Sunday
    assert (sunday - week_start).days == 6


# ===================================================================
# _fetch_sales_tsv — happy / 404 / 401
# ===================================================================

def _mock_response(status: int = 200, content: bytes = b"") -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.content = content
    return m


def test_fetch_sales_tsv_returns_decompressed_bytes():
    """Mock _http to return gzipped TSV → asc returns the raw bytes."""
    gz = gzip.compress(SAMPLE_TSV)
    with patch.object(
        asc._http,
        "fetch_with_retry",
        return_value=_mock_response(200, gz),
    ) as mock_http:
        result = asc._fetch_sales_tsv(
            vendor="94183271", token="tok", target_sunday=dt.date(2026, 5, 17),
        )
    assert result == SAMPLE_TSV
    # Verify Reporter URL и form-body shape
    call = mock_http.call_args
    assert call.kwargs["url"] == asc._REPORTER_URL
    assert call.kwargs["method"] == "POST"
    headers = call.kwargs["headers"]
    # HOTFIX 2026-05-15: token goes in jsonRequest body as accesstoken field,
    # NOT in Authorization HTTP header (Apple Reporter Legacy spec).
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    body = call.kwargs["data"]
    assert body.startswith("jsonRequest=")
    # Token must be inside jsonRequest body
    assert "accesstoken" in body
    assert "tok" in body
    # Verify queryInput contains the expected Sunday in YYYYMMDD
    assert "20260517" in body
    # Vendor encoded
    assert "94183271" in body


def test_fetch_sales_tsv_404_returns_empty_bytes():
    """Apple lag — report not ready yet → empty bytes, no raise."""
    with patch.object(
        asc._http,
        "fetch_with_retry",
        return_value=_mock_response(404, b""),
    ):
        result = asc._fetch_sales_tsv(
            vendor="94183271", token="tok", target_sunday=dt.date(2026, 5, 17),
        )
    assert result == b""


def test_fetch_sales_tsv_401_raises_runtimeerror():
    # HOTFIX: error message now includes HTTP status + response excerpt.
    resp = _mock_response(401, b"")
    resp.text = '{"error":"invalid_token"}'
    with patch.object(asc._http, "fetch_with_retry", return_value=resp):
        with pytest.raises(RuntimeError, match=r"Apple Reporter auth failed \(HTTP 401\)"):
            asc._fetch_sales_tsv(
                vendor="94183271", token="tok",
                target_sunday=dt.date(2026, 5, 17),
            )


def test_fetch_sales_tsv_403_raises_runtimeerror():
    resp = _mock_response(403, b"")
    resp.text = "Forbidden"
    with patch.object(asc._http, "fetch_with_retry", return_value=resp):
        with pytest.raises(RuntimeError, match=r"Apple Reporter auth failed \(HTTP 403\)"):
            asc._fetch_sales_tsv(
                vendor="94183271", token="tok",
                target_sunday=dt.date(2026, 5, 17),
            )


# HOTFIX regression tests (smoke run 25890122345)


def test_fetch_sales_tsv_token_in_body_not_header():
    """Apple Reporter Legacy: token MUST be in jsonRequest body, NOT header."""
    import json as _json
    import urllib.parse as _up
    gz = gzip.compress(SAMPLE_TSV)
    with patch.object(
        asc._http,
        "fetch_with_retry",
        return_value=_mock_response(200, gz),
    ) as mock_http:
        asc._fetch_sales_tsv(
            vendor="94183271", token="secret-token-xyz",
            target_sunday=dt.date(2026, 5, 17),
        )
    body = mock_http.call_args.kwargs["data"]
    headers = mock_http.call_args.kwargs["headers"]
    # NO Authorization header
    assert "Authorization" not in headers
    # Parse jsonRequest body
    encoded = body.replace("jsonRequest=", "", 1)
    parsed = _json.loads(_up.unquote(encoded))
    assert parsed["accesstoken"] == "secret-token-xyz"
    assert parsed["account"] == "94183271"
    assert parsed["mode"] == "Robot.XML"


def test_fetch_sales_tsv_strips_whitespace_in_vendor_and_token():
    """GH Secret storage may add trailing newline — strip before signing."""
    import json as _json
    import urllib.parse as _up
    gz = gzip.compress(SAMPLE_TSV)
    with patch.object(
        asc._http,
        "fetch_with_retry",
        return_value=_mock_response(200, gz),
    ) as mock_http:
        asc._fetch_sales_tsv(
            vendor="94183271\n", token="  token-xyz \n ",
            target_sunday=dt.date(2026, 5, 17),
        )
    body = mock_http.call_args.kwargs["data"]
    encoded = body.replace("jsonRequest=", "", 1)
    parsed = _json.loads(_up.unquote(encoded))
    assert parsed["accesstoken"] == "token-xyz"
    assert parsed["account"] == "94183271"


def test_fetch_sales_tsv_gzip_corruption_raises_runtimeerror():
    """Body must be gzipped — raw text triggers OSError → wrap as RuntimeError."""
    with patch.object(
        asc._http,
        "fetch_with_retry",
        return_value=_mock_response(200, b"not gzipped bytes"),
    ):
        with pytest.raises(RuntimeError, match="gzip decompress"):
            asc._fetch_sales_tsv(
                vendor="94183271", token="tok",
                target_sunday=dt.date(2026, 5, 17),
            )


# ===================================================================
# _parse_installs_from_tsv
# ===================================================================

def test_parse_installs_from_tsv_filters_1F_only():
    """Centry sample: 1F RU 5 + 1F KZ 2 = 7 (3F update row excluded)."""
    total, by_cc = asc._parse_installs_from_tsv(SAMPLE_TSV, APPLE_ID_CENTRY)
    assert total == 7
    assert by_cc == {"RU": 5, "KZ": 2}


def test_parse_installs_from_tsv_groups_by_country():
    """Centry — multiple countries → grouped dict."""
    total, by_cc = asc._parse_installs_from_tsv(SAMPLE_TSV, APPLE_ID_CENTRY)
    assert set(by_cc.keys()) == {"RU", "KZ"}
    assert sum(by_cc.values()) == total


def test_parse_installs_from_tsv_for_diktum_only_1F_counted():
    """Diktum sample: 1F RU 3 = 3 (paid row "1" and IA1 row excluded)."""
    total, by_cc = asc._parse_installs_from_tsv(SAMPLE_TSV, APPLE_ID_DIKTUM)
    assert total == 3
    assert by_cc == {"RU": 3}


def test_parse_installs_from_tsv_empty_tsv_returns_none():
    """tsv_bytes == b'' → (None, {})."""
    total, by_cc = asc._parse_installs_from_tsv(b"", APPLE_ID_CENTRY)
    assert total is None
    assert by_cc == {}


def test_parse_installs_from_tsv_header_only_returns_none():
    """Empty file (only header line) → (None, {})."""
    total, by_cc = asc._parse_installs_from_tsv(EMPTY_TSV, APPLE_ID_CENTRY)
    assert total is None
    assert by_cc == {}


def test_parse_installs_from_tsv_unknown_app_id_returns_zero():
    """File has data, но ни одной строки для запрошенного app_id → (0, {})."""
    total, by_cc = asc._parse_installs_from_tsv(SAMPLE_TSV, "9999999999")
    assert total == 0
    assert by_cc == {}


def test_parse_installs_from_tsv_skips_zero_units_rows():
    """IA1 row for Diktum has Units=0 → not counted, но matched_any_row=True."""
    total, by_cc = asc._parse_installs_from_tsv(SAMPLE_TSV, APPLE_ID_DIKTUM)
    # Only 1F RU 3 counted; paid "1" row and IA1 zero excluded.
    assert total == 3
    assert "BY" not in by_cc   # paid app row excluded


# ===================================================================
# _top_country
# ===================================================================

def test_top_country_picks_max_share():
    """RU 5, KZ 2 → top=RU, share=5/7."""
    top, share = asc._top_country({"RU": 5, "KZ": 2})
    assert top == "RU"
    assert share == pytest.approx(5 / 7)


def test_top_country_single_country_share_is_one():
    top, share = asc._top_country({"RU": 3})
    assert top == "RU"
    assert share == 1.0


def test_top_country_empty_dict_returns_none_pair():
    assert asc._top_country({}) == (None, None)


def test_top_country_zero_total_returns_none_pair():
    """Defensive: dict with zero values → no top."""
    assert asc._top_country({"RU": 0, "KZ": 0}) == (None, None)


# ===================================================================
# _fetch_rss_ratings
# ===================================================================

def test_fetch_rss_ratings_aggregates_across_countries():
    """5 reviews in RU (5+4+5), repeated in US (3 ratings) — weighted avg."""
    def fake_fetch(url: str, method: str = "GET", **kwargs):
        # Same fixture for both RU and US → 3 entries each, all 5,4,5.
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru", "us"],
        )
    # Each country: 3 ratings (5,4,5) = sum 14
    # Across 2 countries: sum=28, count=6 → avg=28/6
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
    responses_by_url: dict[str, MagicMock] = {}

    def fake_fetch(url: str, method: str = "GET", **kwargs):
        m = MagicMock()
        if "/ru/" in url:
            m.status_code = 500
            m.content = b""
            return m
        m.status_code = 200
        m.json.return_value = RSS_CENTRY   # 3 ratings 5,4,5 = sum 14
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
                {"im:rating": {"label": "abc"}},     # invalid
                {"im:rating": {"label": "5"}},        # valid
                {"im:rating": {"label": "9"}},        # out of range — skip
                {"im:rating": "not-a-dict"},          # malformed
                {},                                    # no rating
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
        m.json.return_value = RSS_CENTRY   # sum 14, count 3
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        avg, count = asc._fetch_rss_ratings(
            APPLE_ID_CENTRY, countries=["ru", "us"],
        )
    assert count == 3
    assert avg == pytest.approx(14 / 3)


def test_fetch_rss_ratings_default_countries_includes_ru_us_kz_by_ua():
    """Smoke: with no `countries=` arg, default list includes 5 markets."""
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
# fetch_weekly — full integration with mocked HTTP
# ===================================================================

def test_fetch_weekly_unconfigured_returns_mock(monkeypatch):
    """Without envs → mock StoreSnapshot, no HTTP calls."""
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_weekly("centry", dt.date(2026, 5, 11))
    assert snap.product == "centry"
    assert snap.store == "app_store"
    assert snap.installs == 23
    assert snap.rating == 4.7
    assert snap.top_country == "RU"


def test_fetch_weekly_configured_integrates_real_path(monkeypatch):
    """Full real-mode: TSV + RSS mocked → StoreSnapshot populated."""
    _set_envs(monkeypatch, all_present=True)
    gz_tsv = gzip.compress(SAMPLE_TSV)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        if "reportingitc-reporter.apple.com" in url:
            m.content = gz_tsv
        elif "itunes.apple.com" in url and "/ru/" in url:
            m.json.return_value = RSS_CENTRY
        else:
            # Other RSS countries empty
            m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", dt.date(2026, 5, 11))

    assert snap.product == "centry"
    assert snap.store == "app_store"
    assert snap.week_start == dt.date(2026, 5, 11)
    # SAMPLE_TSV Centry: 1F RU 5 + 1F KZ 2 = 7
    assert snap.installs == 7
    assert snap.top_country == "RU"
    assert snap.top_country_share == pytest.approx(5 / 7)
    # RSS RU only — 3 ratings (5+4+5)/3 = 4.6...
    assert snap.rating == pytest.approx(14 / 3)
    assert snap.error is None


def test_fetch_weekly_404_graceful_degrade(monkeypatch):
    """Reporter 404 → installs None, no error string (lag absorbed)."""
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        if "reportingitc-reporter.apple.com" in url:
            m.status_code = 404
            m.content = b""
        else:
            m.status_code = 200
            m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", dt.date(2026, 5, 11))

    assert snap.installs is None
    assert snap.top_country is None
    assert snap.top_country_share is None
    # 404 == lag, не auth fail → error stays None
    assert snap.error is None


def test_fetch_weekly_401_records_error(monkeypatch):
    """401 wraps to error="reporter auth failed: ..." on StoreSnapshot."""
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 401
        m.content = b""
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", dt.date(2026, 5, 11))

    assert snap.installs is None
    assert snap.error is not None
    assert "reporter auth failed" in snap.error


def test_fetch_previous_unconfigured_returns_mock(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_previous("diktum", dt.date(2026, 5, 11))
    assert snap.installs == 22   # _MOCK_PREV
    assert snap.week_start == dt.date(2026, 5, 4)   # -7 days


def test_fetch_previous_calls_fetch_weekly_with_prev_week(monkeypatch):
    """Configured → shifts target_sunday by -7 days."""
    _set_envs(monkeypatch, all_present=True)
    captured: list[str] = []

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        if "reportingitc-reporter.apple.com" in url:
            captured.append(kwargs.get("data", ""))
            m.status_code = 200
            m.content = gzip.compress(EMPTY_TSV)
        else:
            m.status_code = 200
            m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_previous("centry", dt.date(2026, 5, 11))

    # Prev week of W20 (May 11) is W19 (May 4) → Sunday May 10 → 20260510 in body
    assert any("20260510" in body for body in captured)
    # snapshot.week_start should reflect the PREVIOUS week (Monday May 4),
    # matching mock-mode contract (fetch_previous returns snapshot dated to the
    # earlier week so digest can compare apples-to-apples).
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_weekly_unknown_app_id_returns_zero_installs(monkeypatch):
    """Configured envs, real path, но фиксача без строк для app_id → 0 installs."""
    monkeypatch.setenv("ASC_REPORTER_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ASC_VENDOR_NUMBER", "94183271")
    monkeypatch.setenv("ASC_APP_ID_CENTRY", "9999999999")   # mismatch
    monkeypatch.setenv("ASC_APP_ID_DIKTUM", "8888888888")
    gz_tsv = gzip.compress(SAMPLE_TSV)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        if "reportingitc-reporter.apple.com" in url:
            m.content = gz_tsv
        else:
            m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", dt.date(2026, 5, 11))

    # File had rows, none matched 9999999999 → (0, {})
    assert snap.installs == 0
    assert snap.top_country is None
