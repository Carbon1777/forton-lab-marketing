"""Unit tests for src/store_metrics/asc.py — Analytics Reports installs + iTunes RSS.

After 2026-05-30 (quick 260530-q69):
    - Installs path = ASC Analytics Reports API. JWT ES256 Individual Key
      (sub:"user", aud:"appstoreconnect-v1", NO iss / NO scope). Flow:
      find-or-create ONGOING analyticsReportRequest → reports
      (filter[name]=App Downloads Standard) → instances
      (filter[granularity]=DAILY, processingDate ∈ week) → segments →
      pre-signed url (no auth) → gzip → TSV → sum count column.
      Graceful degradation: no key / no instances yet (24-48h) / API error →
      installs=None + clear error, NEVER raises out of fetch_weekly.
    - Ratings path keeps iTunes Customer Reviews RSS (no auth).

HTTP calls are mocked via unittest.mock.patch on
src.store_metrics._http.fetch_with_retry (ASC API + RSS) and
src.store_metrics.asc.requests.get (segment download — pre-signed url).
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


def _gen_ec_p256_pem() -> str:
    """Throwaway EC P-256 private key (PEM, unencrypted) for JWT signing tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


def _set_installs_envs(monkeypatch, key_id: str = "TESTKEYID01") -> str:
    """Set ASC_KEY_ID + a fresh EC P-256 ASC_PRIVATE_KEY. Returns the key_id."""
    pem = _gen_ec_p256_pem()
    monkeypatch.setenv("ASC_KEY_ID", key_id)
    monkeypatch.setenv("ASC_PRIVATE_KEY", pem)
    asc._reset_jwt_cache()
    return key_id


@pytest.fixture(autouse=True)
def _clean_jwt_cache():
    """Each test starts/ends with a clean module-level JWT cache."""
    asc._reset_jwt_cache()
    yield
    asc._reset_jwt_cache()


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
# installs gate — _installs_configured
# ===================================================================

def test_installs_configured_requires_both_key_envs(monkeypatch):
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)
    assert asc._installs_configured() is False
    monkeypatch.setenv("ASC_KEY_ID", "X")
    assert asc._installs_configured() is False  # private key still missing
    monkeypatch.setenv("ASC_PRIVATE_KEY", "pem")
    assert asc._installs_configured() is True


def test_installs_configured_empty_string_counts_as_missing(monkeypatch):
    monkeypatch.setenv("ASC_KEY_ID", "X")
    monkeypatch.setenv("ASC_PRIVATE_KEY", "")
    assert asc._installs_configured() is False


# ===================================================================
# _asc_jwt — Individual API Key schema
# ===================================================================

def test_asc_jwt_individual_key_schema(monkeypatch):
    """JWT must be sub:user, aud appstoreconnect-v1, ES256, kid=ASC_KEY_ID, NO iss/scope."""
    import jwt as pyjwt

    key_id = _set_installs_envs(monkeypatch, key_id="8SSTB54YPBCY")
    token = asc._asc_jwt()

    payload = pyjwt.decode(token, options={"verify_signature": False})
    header = pyjwt.get_unverified_header(token)

    assert payload["sub"] == "user"
    assert payload["aud"] == "appstoreconnect-v1"
    assert "iss" not in payload
    assert "scope" not in payload
    # iat/exp present, exp within 20 min of iat.
    assert "iat" in payload and "exp" in payload
    assert 0 < payload["exp"] - payload["iat"] <= 1200

    assert header["kid"] == key_id
    assert header["alg"] == "ES256"
    assert header["typ"] == "JWT"


def test_asc_jwt_is_cached(monkeypatch):
    """Second call within margin returns the identical cached token."""
    _set_installs_envs(monkeypatch)
    t1 = asc._asc_jwt()
    t2 = asc._asc_jwt()
    assert t1 == t2


def test_asc_jwt_missing_env_raises(monkeypatch):
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)
    asc._reset_jwt_cache()
    with pytest.raises(RuntimeError, match="ASC_KEY_ID"):
        asc._asc_jwt()


# ===================================================================
# _fetch_installs — happy path + graceful degradation
# ===================================================================

def _gzip_tsv(rows: list[str]) -> bytes:
    """Build a gzip-compressed TSV body from header+data row strings."""
    return gzip.compress("\n".join(rows).encode("utf-8"))


def _mk_resp(status_code: int = 200, json_body: dict | None = None,
             content: bytes | None = None) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    if json_body is not None:
        m.json.return_value = json_body
    if content is not None:
        m.content = content
    return m


def test_fetch_installs_happy_path(monkeypatch):
    """Full chain mocked: reportRequests(list)→reports→instances→segments,
    then a gzip-TSV segment with 2 in-week rows → installs == sum."""
    _set_installs_envs(monkeypatch)
    REQUEST_ID = "req-123"
    REPORT_ID = "rep-456"
    INSTANCE_ID = "inst-789"
    SEGMENT_URL = "https://s3.example.com/segment.gz"

    # TSV: Date \t Counts ; two days inside W20, one outside.
    tsv_rows = [
        "Date\tCounts",
        "2026-05-12\t7",      # in week
        "2026-05-13\t5",      # in week
        "2026-05-30\t100",    # outside week — must be ignored
    ]
    gz_body = _gzip_tsv(tsv_rows)

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={
                "data": [{
                    "id": REQUEST_ID,
                    "attributes": {"accessType": "ONGOING",
                                   "stoppedDueToInactivity": False},
                }],
            })
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            assert params and params.get("filter[name]") == "App Downloads Standard"
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            assert params and params.get("filter[granularity]") == "DAILY"
            return _mk_resp(json_body={"data": [{
                "id": INSTANCE_ID,
                "attributes": {"granularity": "DAILY",
                               "processingDate": "2026-05-13"},
            }]})
        if url.endswith(f"/analyticsReportInstances/{INSTANCE_ID}/segments"):
            return _mk_resp(json_body={"data": [{
                "id": "seg-1",
                "attributes": {"url": SEGMENT_URL, "sizeInBytes": len(gz_body)},
            }]})
        raise AssertionError(f"unexpected url {url}")

    def fake_get(url, timeout=None, **kwargs):
        assert url == SEGMENT_URL
        return _mk_resp(content=gz_body)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch), \
         patch.object(asc.requests, "get", side_effect=fake_get):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert error is None
    assert installs == 5   # processingDate=05-13 → only row for 05-13; 05-12 and 100 excluded


def test_fetch_installs_creates_request_when_none_exists(monkeypatch):
    """No ONGOING request in list → POST creates one, flow proceeds."""
    _set_installs_envs(monkeypatch)
    NEW_REQUEST_ID = "newly-created"

    posted = {"called": False}

    def fake_fetch(url, method="GET", headers=None, params=None, json_body=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": []})  # none exist
        if url.endswith("/analyticsReportRequests") and method == "POST":
            posted["called"] = True
            assert json_body["data"]["attributes"]["accessType"] == "ONGOING"
            return _mk_resp(status_code=201, json_body={"data": {"id": NEW_REQUEST_ID}})
        if url.endswith(f"/analyticsReportRequests/{NEW_REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": []})  # report not ready yet
        raise AssertionError(f"unexpected url {url} method {method}")

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        installs, error = asc._fetch_installs(APPLE_ID_DIKTUM, WEEK_W20)

    assert posted["called"] is True
    assert installs is None
    assert "App Downloads Standard" in error


def test_fetch_installs_no_instances_graceful(monkeypatch):
    """instances empty → (None, error contains 'генерируется'), no raise."""
    _set_installs_envs(monkeypatch)
    REQUEST_ID = "req-1"
    REPORT_ID = "rep-1"

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False},
            }]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            return _mk_resp(json_body={"data": []})  # not generated yet
        raise AssertionError(f"unexpected url {url}")

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert installs is None
    assert error is not None
    assert "генерируется" in error


def test_fetch_installs_no_report_graceful(monkeypatch):
    """reports empty → (None, error about App Downloads Standard)."""
    _set_installs_envs(monkeypatch)
    REQUEST_ID = "req-1"

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False},
            }]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": []})
        raise AssertionError(f"unexpected url {url}")

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert installs is None
    assert "App Downloads Standard" in error


def test_fetch_installs_request_unavailable_graceful(monkeypatch):
    """ensure_ongoing fails (list 500-ish then POST fails) → clear error, no raise."""
    _set_installs_envs(monkeypatch)

    def fake_fetch(url, method="GET", headers=None, params=None, json_body=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(status_code=403, json_body={})
        if url.endswith("/analyticsReportRequests") and method == "POST":
            return _mk_resp(status_code=409, json_body={})  # create fails too
        raise AssertionError(f"unexpected url {url} method {method}")

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert installs is None
    assert "ONGOING request" in error


def test_fetch_installs_never_raises_on_exception(monkeypatch):
    """Unexpected exception AFTER ensure_ongoing → (None, 'ASC installs error: ...').

    ensure_ongoing succeeds (request id), then the reports GET raises an
    unhandled error → caught by the outer guard, never propagates out.
    """
    _set_installs_envs(monkeypatch)

    def fake_ensure(app_id):
        return "req-ok"

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    with patch.object(asc, "_ensure_ongoing_request", side_effect=fake_ensure), \
         patch.object(asc, "_asc_get", side_effect=boom):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert installs is None
    assert error is not None
    assert "ASC installs error" in error


def test_fetch_installs_request_none_returns_ongoing_error(monkeypatch):
    """ensure_ongoing returns None → (None, 'ASC: не удалось получить ONGOING request')."""
    _set_installs_envs(monkeypatch)

    with patch.object(asc, "_ensure_ongoing_request", return_value=None):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert installs is None
    assert "ONGOING request" in error


def test_fetch_installs_sums_multiple_segments(monkeypatch):
    """One instance with two segments → both TSV bodies summed."""
    _set_installs_envs(monkeypatch)
    REQUEST_ID, REPORT_ID, INSTANCE_ID = "r", "rep", "inst"
    URL_A = "https://s3.example.com/a.gz"
    URL_B = "https://s3.example.com/b.gz"
    gz_a = _gzip_tsv(["Date\tCounts", "2026-05-12\t3"])
    gz_b = _gzip_tsv(["Date\tUnits", "2026-05-14\t9"])  # different count column

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False}}]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            return _mk_resp(json_body={"data": [{
                "id": INSTANCE_ID,
                "attributes": {"processingDate": "2026-05-12"}}]})
        if url.endswith(f"/analyticsReportInstances/{INSTANCE_ID}/segments"):
            return _mk_resp(json_body={"data": [
                {"id": "s1", "attributes": {"url": URL_A}},
                {"id": "s2", "attributes": {"url": URL_B}},
            ]})
        raise AssertionError(f"unexpected url {url}")

    def fake_get(url, timeout=None, **kwargs):
        return _mk_resp(content=gz_a if url == URL_A else gz_b)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch), \
         patch.object(asc.requests, "get", side_effect=fake_get):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert error is None
    assert installs == 3   # processingDate=05-12 → seg A(05-12)=3, seg B(05-14) ignored


# ===================================================================
# _parse_segment_tsv — column detection / tolerance
# ===================================================================

def test_parse_segment_tsv_picks_first_matching_count_column():
    text = "Date\tCounts\n2026-05-12\t4\n2026-05-13\t6"
    # Only 05-12 matches target_date → 4, not 10
    assert asc._parse_segment_tsv(text, dt.date(2026, 5, 12)) == 4


def test_parse_segment_tsv_filters_by_target_date():
    text = "Date\tCounts\n2026-05-04\t999\n2026-05-12\t4"  # 05-04 is a different date
    assert asc._parse_segment_tsv(text, dt.date(2026, 5, 12)) == 4


def test_parse_segment_tsv_tolerates_garbage_rows():
    text = (
        "Date\tCounts\n"
        "not-a-date\t5\n"      # bad date
        "2026-05-12\tNaN\n"    # bad count
        "2026-05-12\t8\n"      # good
        "\n"                    # empty
        "short\n"               # too few cols
    )
    assert asc._parse_segment_tsv(text, dt.date(2026, 5, 12)) == 8


def test_parse_segment_tsv_missing_columns_returns_zero():
    text = "Foo\tBar\n2026-05-12\t4"
    assert asc._parse_segment_tsv(text, dt.date(2026, 5, 12)) == 0


def test_parse_segment_tsv_accepts_float_counts():
    text = "Date\tUnits\n2026-05-12\t12.0"
    assert asc._parse_segment_tsv(text, dt.date(2026, 5, 12)) == 12


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
# fetch_weekly — integration
# ===================================================================

def test_fetch_weekly_unconfigured_returns_mock(monkeypatch):
    """Without envs → mock StoreSnapshot, no HTTP / API calls."""
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_weekly("centry", WEEK_W20)
    assert snap.product == "centry"
    assert snap.store == "app_store"
    assert snap.installs == 23
    assert snap.rating == 4.7
    assert snap.top_country == "RU"


def test_fetch_weekly_no_key_installs_none_with_keymsg(monkeypatch):
    """Configured (app-ids) but NO ASC_KEY_ID/ASC_PRIVATE_KEY → installs=None +
    error про ключ; rating всё равно из RSS (independent axis)."""
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.error is not None
    assert "ASC_KEY_ID" in snap.error
    assert "ASC_PRIVATE_KEY" in snap.error
    # Rating still picked up from RSS — independent axis.
    assert snap.rating is not None
    assert snap.top_country is None
    assert snap.top_country_share is None


def test_fetch_weekly_integrates_installs(monkeypatch):
    """Happy-path installs (Analytics) lands in StoreSnapshot.installs, error None."""
    _set_envs(monkeypatch, all_present=True)
    _set_installs_envs(monkeypatch)
    REQUEST_ID, REPORT_ID, INSTANCE_ID = "rq", "rp", "in"
    SEG_URL = "https://s3.example.com/seg.gz"
    gz = _gzip_tsv(["Date\tCounts", "2026-05-12\t11", "2026-05-15\t9"])

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        # RSS calls go through the same fetch_with_retry — route by host.
        if "itunes.apple.com" in url:
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = RSS_CENTRY
            return m
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False}}]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            return _mk_resp(json_body={"data": [{
                "id": INSTANCE_ID,
                "attributes": {"processingDate": "2026-05-15"}}]})
        if url.endswith(f"/analyticsReportInstances/{INSTANCE_ID}/segments"):
            return _mk_resp(json_body={"data": [
                {"id": "s", "attributes": {"url": SEG_URL}}]})
        raise AssertionError(f"unexpected url {url}")

    def fake_get(url, timeout=None, **kwargs):
        return _mk_resp(content=gz)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch), \
         patch.object(asc.requests, "get", side_effect=fake_get):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 9   # processingDate=05-15 → only row for 05-15; 05-12 excluded
    assert snap.error is None
    assert snap.rating is not None  # RSS still works


def test_fetch_weekly_installs_no_instances_graceful(monkeypatch):
    """instances empty → installs=None + 'генерируется', RSS rating still set."""
    _set_envs(monkeypatch, all_present=True)
    _set_installs_envs(monkeypatch)
    REQUEST_ID, REPORT_ID = "rq", "rp"

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if "itunes.apple.com" in url:
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = RSS_CENTRY
            return m
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False}}]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            return _mk_resp(json_body={"data": []})
        raise AssertionError(f"unexpected url {url}")

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.error is not None
    assert "генерируется" in snap.error
    assert snap.rating is not None


def test_fetch_weekly_rss_fails_installs_unaffected(monkeypatch):
    """RSS network failure → rating=None, but installs path independent.

    With no installs key configured, error is the key message; RSS swallowed."""
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)

    def fake_fetch(url, method="GET", **kwargs):
        raise RuntimeError("network down")

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.rating is None
    assert snap.error is not None
    assert "ASC_KEY_ID" in snap.error


def test_fetch_weekly_for_diktum_isolates_correctly(monkeypatch):
    """Same envs, requesting Diktum → snapshot built for diktum app_id."""
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_weekly("diktum", WEEK_W20)

    assert snap.product == "diktum"
    assert snap.store == "app_store"
    assert snap.installs is None
    assert snap.rating is None  # empty RSS fixture for Diktum
    assert snap.error is not None


# ===================================================================
# fetch_previous
# ===================================================================

def test_fetch_previous_unconfigured_returns_mock(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_previous("diktum", WEEK_W20)
    assert snap.installs == 22   # _MOCK_PREV
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_previous_shifts_week_by_7_days(monkeypatch):
    """Configured (no installs key) → fetch_weekly called with week_start - 7 days."""
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch):
        snap = asc.fetch_previous("centry", WEEK_W20)

    assert snap.week_start == dt.date(2026, 5, 4)
    assert snap.installs is None
    assert snap.error is not None


# ===================================================================
# Regression: no duplicate count across instances (per-date filtering)
# ===================================================================

def test_fetch_installs_no_duplicate_count_across_instances(monkeypatch):
    """Two instances, each TSV has rows for BOTH processing dates.

    Without per-date filtering, each instance would count the other
    instance's rows too → 36 instead of correct 18.
    With per-date filtering: inst-1(proc=06-01) → only 06-01 row = 10;
    inst-2(proc=06-02) → only 06-02 row = 8; total = 18.
    """
    _set_installs_envs(monkeypatch)
    REQUEST_ID = "req-reg"
    REPORT_ID = "rep-reg"
    INST_ID_1 = "inst-reg-1"
    INST_ID_2 = "inst-reg-2"
    URL_1 = "https://s3.example.com/reg1.gz"
    URL_2 = "https://s3.example.com/reg2.gz"

    # Both TSVs contain rows for BOTH dates (simulating Apple including all days)
    gz_both = _gzip_tsv(["Date\tCounts", "2026-06-01\t10", "2026-06-02\t8"])

    WEEK_START = dt.date(2026, 6, 1)   # Mon 2026-06-01; 06-01 and 06-02 are Mon+Tue of this week

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False},
            }]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            return _mk_resp(json_body={"data": [
                {"id": INST_ID_1, "attributes": {"processingDate": "2026-06-01"}},
                {"id": INST_ID_2, "attributes": {"processingDate": "2026-06-02"}},
            ]})
        if url.endswith(f"/analyticsReportInstances/{INST_ID_1}/segments"):
            return _mk_resp(json_body={"data": [
                {"id": "s1", "attributes": {"url": URL_1}},
            ]})
        if url.endswith(f"/analyticsReportInstances/{INST_ID_2}/segments"):
            return _mk_resp(json_body={"data": [
                {"id": "s2", "attributes": {"url": URL_2}},
            ]})
        raise AssertionError(f"unexpected url {url}")

    def fake_get(url, timeout=None, **kwargs):
        return _mk_resp(content=gz_both)  # same body for both instances

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch), \
         patch.object(asc.requests, "get", side_effect=fake_get):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_START)

    assert error is None
    assert installs == 18  # 10 (06-01) + 8 (06-02), NOT 36


# ===================================================================
# fetch_weekly — missing app_id env degrades gracefully
# ===================================================================

def test_fetch_weekly_missing_app_id_env_degrades_gracefully(monkeypatch):
    """ASC_APP_ID_LAPULYA not set → StoreSnapshot(installs=None, error с именем env).

    Base envs (CENTRY/DIKTUM app-ids) are set, but lapulya is absent.
    fetch_weekly("lapulya", ...) must return a snapshot, not raise RuntimeError.
    """
    _set_envs(monkeypatch, all_present=True)
    _set_installs_envs(monkeypatch)
    monkeypatch.delenv("ASC_APP_ID_LAPULYA", raising=False)

    snap = asc.fetch_weekly("lapulya", WEEK_W20)

    assert snap.product == "lapulya"
    assert snap.store == "app_store"
    assert snap.installs is None
    assert snap.error is not None
    assert "ASC_APP_ID_LAPULYA" in snap.error


# ===================================================================
# fetch_monthly + _fetch_installs_range — месячный контур (260611-8za)
# ===================================================================

def test_fetch_monthly_unconfigured_returns_mock(monkeypatch):
    """Without envs → mock snapshot, week_start = первое число месяца."""
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_monthly("centry", 2026, 5)
    assert snap.product == "centry"
    assert snap.store == "app_store"
    assert snap.week_start == dt.date(2026, 5, 1)
    assert snap.installs == asc._MOCK_INSTALLS["centry"]
    assert snap.rating == 4.7


def test_month_range_regular_and_leap():
    assert asc._month_range(2026, 5) == (dt.date(2026, 5, 1), dt.date(2026, 5, 31))
    assert asc._month_range(2026, 2) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert asc._month_range(2028, 2) == (dt.date(2028, 2, 1), dt.date(2028, 2, 29))
    assert asc._month_range(2026, 12) == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))


def test_fetch_installs_range_filters_instances_by_month(monkeypatch):
    """Instances вне месяца не качаются; in-month суммируются per-day."""
    _set_installs_envs(monkeypatch)
    REQUEST_ID = "req-m"
    REPORT_ID = "rep-m"
    IN_MONTH_ID = "inst-may"
    OUT_MONTH_ID = "inst-june"
    SEGMENT_URL = "https://s3.example.com/seg-may.gz"

    tsv_rows = [
        "Date\tCounts",
        "2026-05-15\t7",      # matches instance proc_date → counted
        "2026-05-16\t3",      # другой день — per-instance фильтр отбросит
    ]
    gz_body = _gzip_tsv(tsv_rows)
    segment_calls: list[str] = []

    def fake_fetch(url, method="GET", headers=None, params=None, **kwargs):
        if url.endswith("/analyticsReportRequests") and method == "GET":
            return _mk_resp(json_body={"data": [{
                "id": REQUEST_ID,
                "attributes": {"accessType": "ONGOING",
                               "stoppedDueToInactivity": False},
            }]})
        if url.endswith(f"/analyticsReportRequests/{REQUEST_ID}/reports"):
            return _mk_resp(json_body={"data": [{"id": REPORT_ID}]})
        if url.endswith(f"/analyticsReports/{REPORT_ID}/instances"):
            return _mk_resp(json_body={"data": [
                {"id": IN_MONTH_ID,
                 "attributes": {"granularity": "DAILY",
                                "processingDate": "2026-05-15"}},
                {"id": OUT_MONTH_ID,
                 "attributes": {"granularity": "DAILY",
                                "processingDate": "2026-06-02"}},
            ]})
        if url.endswith(f"/analyticsReportInstances/{IN_MONTH_ID}/segments"):
            segment_calls.append(IN_MONTH_ID)
            return _mk_resp(json_body={"data": [{
                "id": "seg-1",
                "attributes": {"url": SEGMENT_URL, "sizeInBytes": len(gz_body)},
            }]})
        if url.endswith(f"/analyticsReportInstances/{OUT_MONTH_ID}/segments"):
            raise AssertionError("out-of-month instance must not be downloaded")
        raise AssertionError(f"unexpected url {url}")

    def fake_get(url, timeout=None, **kwargs):
        assert url == SEGMENT_URL
        return _mk_resp(content=gz_body)

    with patch.object(asc._http, "fetch_with_retry", side_effect=fake_fetch), \
         patch.object(asc.requests, "get", side_effect=fake_get):
        installs, error = asc._fetch_installs_range(
            APPLE_ID_CENTRY, dt.date(2026, 5, 1), dt.date(2026, 5, 31),
        )

    assert error is None
    assert installs == 7   # только строка за proc_date instance (2026-05-15)
    assert segment_calls == [IN_MONTH_ID]


def test_fetch_installs_week_wrapper_delegates_seven_day_range(monkeypatch):
    """_fetch_installs(week_start) == range(week_start, week_start+6) — контракт недельного контура."""
    captured: dict[str, object] = {}

    def fake_range(app_id, start_date, end_date):
        captured["args"] = (app_id, start_date, end_date)
        return (42, None)

    with patch.object(asc, "_fetch_installs_range", side_effect=fake_range):
        installs, error = asc._fetch_installs(APPLE_ID_CENTRY, WEEK_W20)

    assert installs == 42
    assert error is None
    assert captured["args"] == (
        APPLE_ID_CENTRY, WEEK_W20, WEEK_W20 + dt.timedelta(days=6),
    )


def test_fetch_monthly_no_installs_key_returns_error(monkeypatch):
    """App-id envs есть, ключа installs нет → installs=None + no-key error."""
    _set_envs(monkeypatch, all_present=True)
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    monkeypatch.delenv("ASC_PRIVATE_KEY", raising=False)

    with patch.object(asc, "_fetch_rss_ratings", return_value=(4.5, 10)):
        snap = asc.fetch_monthly("centry", 2026, 5)

    assert snap.week_start == dt.date(2026, 5, 1)
    assert snap.installs is None
    assert snap.error == asc._INSTALLS_NO_KEY_ERROR
    assert snap.rating == pytest.approx(4.5)


def test_fetch_monthly_configured_uses_month_range(monkeypatch):
    """Configured → _fetch_installs_range зовётся с [1-е, последнее число месяца]."""
    _set_envs(monkeypatch, all_present=True)
    _set_installs_envs(monkeypatch)
    captured: dict[str, object] = {}

    def fake_range(app_id, start_date, end_date):
        captured["args"] = (app_id, start_date, end_date)
        return (55, None)

    with patch.object(asc, "_fetch_installs_range", side_effect=fake_range), \
         patch.object(asc, "_fetch_rss_ratings", return_value=(None, 0)):
        snap = asc.fetch_monthly("centry", 2026, 5)

    assert captured["args"] == (
        APPLE_ID_CENTRY, dt.date(2026, 5, 1), dt.date(2026, 5, 31),
    )
    assert snap.installs == 55
    assert snap.error is None
    assert snap.week_start == dt.date(2026, 5, 1)
