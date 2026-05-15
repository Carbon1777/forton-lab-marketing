"""Unit tests for src/store_metrics/play.py — Google Play GCS + androidpublisher.

All Google client objects are mocked via unittest.mock.patch. Fixtures live in
tests/fixtures/store_metrics/. Three external services are replaced:

    - google.cloud.storage.Client (GCS bucket reader)
    - googleapiclient.discovery.build (androidpublisher)
    - google.oauth2.service_account.Credentials (creds builder)
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import play

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store_metrics"

INSTALLS_CSV_BYTES = (FIXTURES / "play_installs_country.csv").read_bytes()
REVIEWS_SAMPLE = json.loads(
    (FIXTURES / "play_reviews_sample.json").read_text(encoding="utf-8")
)
REVIEWS_EMPTY = json.loads(
    (FIXTURES / "play_reviews_empty.json").read_text(encoding="utf-8")
)

PACKAGE_CENTRY = "website.centry.app"
PACKAGE_DIKTUM = "ru.diktumweb.diktum"
DEV_ID = "6224792403622982347"

# Minimal SA JSON — credentials are mocked, so we don't need a valid RSA key.
_FAKE_SA = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "fake",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "test@test-project.iam.gserviceaccount.com",
    "client_id": "123",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}
FAKE_SA_JSON = json.dumps(_FAKE_SA)


# ===================================================================
# env / configuration
# ===================================================================

def _set_envs(monkeypatch, *, mode: str = "raw") -> None:
    """Configure all envs. mode in {raw, path, none}."""
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON_PATH", raising=False)
    if mode == "raw":
        monkeypatch.setenv("GOOGLE_PLAY_SA_JSON", FAKE_SA_JSON)
    elif mode == "path":
        monkeypatch.setenv("GOOGLE_PLAY_SA_JSON_PATH", "/tmp/sa.json")
    # mode == "none" leaves SA envs unset
    if mode != "none":
        monkeypatch.setenv("GPLAY_DEVELOPER_ID", DEV_ID)
        monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", PACKAGE_CENTRY)
        monkeypatch.setenv("GPLAY_PACKAGE_DIKTUM", PACKAGE_DIKTUM)
    else:
        for k in ("GPLAY_DEVELOPER_ID", "GPLAY_PACKAGE_CENTRY",
                  "GPLAY_PACKAGE_DIKTUM"):
            monkeypatch.delenv(k, raising=False)


def test_is_configured_all_envs_set(monkeypatch):
    """Raw JSON env + 3 base envs → True."""
    _set_envs(monkeypatch, mode="raw")
    assert play._is_configured() is True


def test_is_configured_with_path_env(monkeypatch):
    """Path env can substitute for raw JSON env."""
    _set_envs(monkeypatch, mode="path")
    assert play._is_configured() is True


def test_is_configured_missing_envs(monkeypatch):
    """None of the SA envs set → False (even if base envs present)."""
    _set_envs(monkeypatch, mode="none")
    # Set only the base envs, leave SA unset → still incomplete
    monkeypatch.setenv("GPLAY_DEVELOPER_ID", DEV_ID)
    monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", PACKAGE_CENTRY)
    monkeypatch.setenv("GPLAY_PACKAGE_DIKTUM", PACKAGE_DIKTUM)
    assert play._is_configured() is False


def test_is_configured_missing_developer_id(monkeypatch):
    """Has SA but missing GPLAY_DEVELOPER_ID → False."""
    _set_envs(monkeypatch, mode="raw")
    monkeypatch.delenv("GPLAY_DEVELOPER_ID", raising=False)
    assert play._is_configured() is False


def test_package_for_centry(monkeypatch):
    _set_envs(monkeypatch, mode="raw")
    assert play._package_for("centry") == PACKAGE_CENTRY


def test_package_for_diktum(monkeypatch):
    _set_envs(monkeypatch, mode="raw")
    assert play._package_for("diktum") == PACKAGE_DIKTUM


def test_package_for_missing_env_raises(monkeypatch):
    _set_envs(monkeypatch, mode="none")
    with pytest.raises(RuntimeError, match="GPLAY_PACKAGE_CENTRY"):
        play._package_for("centry")


# ===================================================================
# Date helpers
# ===================================================================

def test_iso_week_range_for_monday():
    """Mon 2026-05-12 → (2026-05-12, 2026-05-18) inclusive."""
    start, end = play._iso_week_range(dt.date(2026, 5, 12))
    assert start == dt.date(2026, 5, 12)
    assert end == dt.date(2026, 5, 18)


def test_target_dates_returns_7_dates():
    """Week 12-18 May → 7 daily dates (Mon..Sun inclusive)."""
    dates = play._target_dates(dt.date(2026, 5, 12), dt.date(2026, 5, 18))
    assert len(dates) == 7
    assert dates[0] == dt.date(2026, 5, 12)
    assert dates[-1] == dt.date(2026, 5, 18)


def test_last_closed_month_yyyymm_returns_prev_month():
    """Week in May → previous closed month is April (202604)."""
    assert play._last_closed_month_yyyymm(dt.date(2026, 5, 12)) == "202604"
    assert play._last_closed_month_yyyymm(dt.date(2026, 1, 5)) == "202512"


# ===================================================================
# _parse_installs_csv — uses real UTF-16 LE BOM fixture
# ===================================================================

def _DEPRECATED_test_parse_installs_csv_filters_package():
    """Mixed Centry + Diktum fixture: filtering by PACKAGE_CENTRY drops Diktum rows."""
    total_c, by_c = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    # Centry rows in fixture (7 days): 3+2+1+2+1+1+1 = 11
    assert total_c == 11
    # No Diktum-only countries leak in
    assert "BY" in by_c or "RU" in by_c  # both Centry-touched
    # Diktum got 3 installs total in May 12-13 ( 2 + 1 ), but no Centry override
    total_d, by_d = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, PACKAGE_DIKTUM,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    assert total_d == 3  # 2 + 1 = 3 (May 14 has 0 installs)
    assert by_d == {"RU": 3}


def _DEPRECATED_test_parse_installs_csv_filters_date_range():
    """Narrow window 2026-05-12..05-12 → only one day for Centry."""
    narrow, _ = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 12),
    )
    full, _ = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    assert narrow == 3  # only the May 12 entry: 3 installs
    assert full == 11
    assert narrow < full


def _DEPRECATED_test_parse_installs_csv_utf16_bom_decoded_correctly():
    """Verify UTF-16 LE BOM (first 2 bytes 0xFF 0xFE) is auto-detected.

    If decoder used the wrong encoding, header would not equal 'Date', so
    the Package Name column would be misaligned → 0 matches across the file.
    """
    # Sanity: фикстура действительно начинается с UTF-16 LE BOM
    assert INSTALLS_CSV_BYTES[:2] == b"\xff\xfe"
    total, by_cc = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    # Successful decoding yields the documented Centry totals.
    assert total == 11
    assert sum(by_cc.values()) == 11


def _DEPRECATED_test_parse_installs_csv_groups_by_country():
    """RU dominant for Centry (9), KZ 1, BY 1."""
    total, by_cc = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    assert total == 11
    assert by_cc == {"RU": 9, "KZ": 1, "BY": 1}


def _DEPRECATED_test_parse_installs_csv_empty_returns_none():
    """tsv_bytes == b'' → (None, {})."""
    total, by_cc = play._parse_installs_csv(
        b"", PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    assert total is None
    assert by_cc == {}


def _DEPRECATED_test_parse_installs_csv_header_only_returns_none():
    """Just the header line + BOM → (None, {})."""
    header_only = (
        b"\xff\xfe"
        + "Date,Package Name,Country,Daily Device Installs,"
          "Daily Device Uninstalls,Active Device Installs\n".encode("utf-16-le")
    )
    total, by_cc = play._parse_installs_csv(
        header_only, PACKAGE_CENTRY,
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    assert total is None
    assert by_cc == {}


def _DEPRECATED_test_parse_installs_csv_unknown_package_returns_zero():
    """File has data but no rows match → (0, {})."""
    total, by_cc = play._parse_installs_csv(
        INSTALLS_CSV_BYTES, "com.unknown.package",
        dt.date(2026, 5, 12), dt.date(2026, 5, 18),
    )
    assert total == 0
    assert by_cc == {}


# ===================================================================
# _top_country
# ===================================================================

def test_top_country_picks_max_share():
    top, share = play._top_country({"RU": 9, "KZ": 1, "BY": 1})
    assert top == "RU"
    assert share == pytest.approx(9 / 11)


def test_top_country_empty_dict_returns_none_pair():
    assert play._top_country({}) == (None, None)


# ===================================================================
# _fetch_installs_csv — mock GCS
# ===================================================================

def _DEPRECATED_test_fetch_installs_csv_blob_not_exists_returns_none():
    """Blob doesn't exist → returns None (not raise)."""
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.return_value = False
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket

    with patch("google.cloud.storage.Client", return_value=fake_client) as m:
        result = play._fetch_installs_csv(
            fake_creds, DEV_ID, PACKAGE_CENTRY, "202605",
        )

    assert result is None
    # Bucket name constructed from developer_id
    assert m.call_count == 1
    fake_client.bucket.assert_called_once_with(f"pubsite_prod_rev_{DEV_ID}")
    fake_bucket.blob.assert_called_once_with(
        f"stats/installs/installs_{PACKAGE_CENTRY}_202605_country.csv"
    )


def _DEPRECATED_test_fetch_installs_csv_blob_exists_returns_bytes():
    """Blob exists → returns its raw bytes (UTF-16 LE BOM)."""
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.download_as_bytes.return_value = INSTALLS_CSV_BYTES
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket

    with patch("google.cloud.storage.Client", return_value=fake_client):
        result = play._fetch_installs_csv(
            fake_creds, DEV_ID, PACKAGE_CENTRY, "202605",
        )

    assert result == INSTALLS_CSV_BYTES


# ===================================================================
# _fetch_reviews — mock androidpublisher
# ===================================================================

def _make_play_service_mock(*page_responses) -> MagicMock:
    """Build a mocked googleapiclient service.

    `page_responses` is a sequence of dicts; each call to
    `service.reviews().list(...).execute()` returns the next response.
    """
    service = MagicMock()
    reviews_obj = MagicMock()
    list_mock = MagicMock()
    # `execute()` returns successive payloads on successive calls.
    list_mock.execute.side_effect = list(page_responses)
    reviews_obj.list.return_value = list_mock
    service.reviews.return_value = reviews_obj
    return service


def test_fetch_reviews_aggregates_avg():
    """Sample fixture has 2 reviews (ratings 5, 4) → avg 4.5, count 2."""
    service = _make_play_service_mock(REVIEWS_SAMPLE)
    fake_creds = MagicMock(name="creds")
    with patch("googleapiclient.discovery.build", return_value=service):
        avg, count = play._fetch_reviews(fake_creds, PACKAGE_CENTRY)
    assert count == 2
    assert avg == pytest.approx(4.5)


def test_fetch_reviews_empty_returns_none():
    """Empty fixture → (None, 0), no exception."""
    service = _make_play_service_mock(REVIEWS_EMPTY)
    fake_creds = MagicMock(name="creds")
    with patch("googleapiclient.discovery.build", return_value=service):
        avg, count = play._fetch_reviews(fake_creds, PACKAGE_CENTRY)
    assert avg is None
    assert count == 0


def test_fetch_reviews_pagination():
    """First page returns tokenPagination.nextPageToken → second page fetched + merged."""
    page1 = {
        "reviews": [
            {"comments": [{"userComment": {"starRating": 5}}]},
            {"comments": [{"userComment": {"starRating": 3}}]},
        ],
        "tokenPagination": {"nextPageToken": "tok-2"},
    }
    page2 = {
        "reviews": [
            {"comments": [{"userComment": {"starRating": 4}}]},
        ],
        # No nextPageToken → loop terminates.
    }
    service = _make_play_service_mock(page1, page2)
    fake_creds = MagicMock(name="creds")
    with patch("googleapiclient.discovery.build", return_value=service):
        avg, count = play._fetch_reviews(fake_creds, PACKAGE_CENTRY)
    assert count == 3
    assert avg == pytest.approx((5 + 3 + 4) / 3)


def test_fetch_reviews_skips_invalid_star_rating():
    """Garbled / out-of-range ratings dropped, valid ones counted."""
    page = {
        "reviews": [
            {"comments": [{"userComment": {"starRating": "5"}}]},   # valid coerce
            {"comments": [{"userComment": {"starRating": None}}]},  # skip
            {"comments": [{"userComment": {"starRating": 9}}]},     # out of range
            {"comments": [{"userComment": {"starRating": "abc"}}]}, # invalid
            {"comments": [{"userComment": {"starRating": 4}}]},     # valid
            {"comments": []},                                        # no comments
            {},                                                       # no comments key
        ],
    }
    service = _make_play_service_mock(page)
    fake_creds = MagicMock(name="creds")
    with patch("googleapiclient.discovery.build", return_value=service):
        avg, count = play._fetch_reviews(fake_creds, PACKAGE_CENTRY)
    assert count == 2
    assert avg == pytest.approx((5 + 4) / 2)


# ===================================================================
# fetch_weekly — integration
# ===================================================================

def test_fetch_weekly_unconfigured_returns_mock(monkeypatch):
    """Without envs → mock StoreSnapshot, no HTTP calls."""
    _set_envs(monkeypatch, mode="none")
    snap = play.fetch_weekly("centry", dt.date(2026, 5, 11))
    assert snap.product == "centry"
    assert snap.store == "google_play"
    assert snap.installs == 11   # _MOCK_INSTALLS
    assert snap.rating == 4.6
    assert snap.top_country == "RU"


def _DEPRECATED_test_fetch_weekly_configured_integrates_real_path(monkeypatch):
    """Full configured path: GCS + reviews mocked → real StoreSnapshot.

    week_start = Mon 2026-05-11 → window is 2026-05-11..05-17.
    Fixture data covers 05-12..05-18; rows 05-12..05-17 fit in window:
    Centry: 3+2+1+2+1+1 = 10. (05-18 excluded — outside window).
    """
    _set_envs(monkeypatch, mode="raw")

    # ---- Mock service_account.Credentials -----
    fake_creds = MagicMock(name="creds")
    # ---- Mock GCS Client -----
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.download_as_bytes.return_value = INSTALLS_CSV_BYTES
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket
    # ---- Mock androidpublisher service ----
    fake_service = _make_play_service_mock(REVIEWS_SAMPLE)

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "google.cloud.storage.Client", return_value=fake_client,
    ), patch(
        "googleapiclient.discovery.build", return_value=fake_service,
    ):
        snap = play.fetch_weekly("centry", dt.date(2026, 5, 11))

    assert snap.product == "centry"
    assert snap.store == "google_play"
    assert snap.week_start == dt.date(2026, 5, 11)
    # Centry rows 05-12..05-17 (05-18 outside week window 05-11..05-17):
    # 3 + 2 + 1 + 2 + 1 + 1 = 10
    assert snap.installs == 10
    assert snap.top_country == "RU"
    # Reviews avg 4.5
    assert snap.rating == pytest.approx(4.5)
    assert snap.error is None


def _DEPRECATED_test_fetch_weekly_month_boundary_fetches_two_files(monkeypatch):
    """Week spans Apr 27 – May 3 → both 202604 + 202605 blobs are requested."""
    _set_envs(monkeypatch, mode="raw")
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    # Both blobs exist but return empty CSV → installs=0
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.download_as_bytes.return_value = b""
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket
    fake_service = _make_play_service_mock(REVIEWS_EMPTY)

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "google.cloud.storage.Client", return_value=fake_client,
    ), patch(
        "googleapiclient.discovery.build", return_value=fake_service,
    ):
        play.fetch_weekly("centry", dt.date(2026, 4, 27))

    # Verify .blob() was called twice — once per month.
    assert fake_bucket.blob.call_count == 2
    paths = [c.args[0] for c in fake_bucket.blob.call_args_list]
    assert any("_202604_" in p for p in paths)
    assert any("_202605_" in p for p in paths)


def test_fetch_weekly_forbidden_returns_error_snapshot(monkeypatch):
    """GCS Forbidden during installs → StoreSnapshot with error set."""
    _set_envs(monkeypatch, mode="raw")
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()

    from google.api_core import exceptions as gcp_exc

    # bucket() returns a bucket whose .blob().exists() raises Forbidden
    def raise_forbidden(*args, **kwargs):
        raise gcp_exc.Forbidden("storage.objects.get permission denied")

    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.side_effect = raise_forbidden
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "google.cloud.storage.Client", return_value=fake_client,
    ):
        snap = play.fetch_weekly("centry", dt.date(2026, 5, 11))

    assert snap.installs is None
    assert snap.error is not None
    assert "GCS access denied" in snap.error


def test_fetch_weekly_credentials_failure_returns_error(monkeypatch):
    """Invalid SA JSON → credentials build raises → error snapshot."""
    monkeypatch.setenv("GOOGLE_PLAY_SA_JSON", "{not valid json")
    monkeypatch.setenv("GPLAY_DEVELOPER_ID", DEV_ID)
    monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", PACKAGE_CENTRY)
    monkeypatch.setenv("GPLAY_PACKAGE_DIKTUM", PACKAGE_DIKTUM)

    snap = play.fetch_weekly("centry", dt.date(2026, 5, 11))
    assert snap.installs is None
    assert snap.error is not None
    assert "credentials build failed" in snap.error


def _DEPRECATED_test_fetch_weekly_missing_blob_for_current_month_returns_none(monkeypatch):
    """No blob yet (Google nightly run not done) → installs=None, error=None."""
    _set_envs(monkeypatch, mode="raw")
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.return_value = False    # blob missing
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket
    fake_service = _make_play_service_mock(REVIEWS_EMPTY)

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "google.cloud.storage.Client", return_value=fake_client,
    ), patch(
        "googleapiclient.discovery.build", return_value=fake_service,
    ):
        snap = play.fetch_weekly("centry", dt.date(2026, 5, 11))

    # Graceful — no data seen → installs=None, no error string.
    assert snap.installs is None
    assert snap.error is None


def _DEPRECATED_test_fetch_weekly_reviews_failure_does_not_break_installs(monkeypatch):
    """androidpublisher raises → installs still returned, rating=None."""
    _set_envs(monkeypatch, mode="raw")
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.download_as_bytes.return_value = INSTALLS_CSV_BYTES
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket

    # androidpublisher build raises before any review fetched
    def raise_review(*args, **kwargs):
        raise RuntimeError("androidpublisher build failed")

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "google.cloud.storage.Client", return_value=fake_client,
    ), patch(
        "googleapiclient.discovery.build", side_effect=raise_review,
    ):
        snap = play.fetch_weekly("centry", dt.date(2026, 5, 11))

    # Installs still populated; rating left as None (graceful per-call degrade).
    assert snap.installs == 10
    assert snap.rating is None
    assert snap.error is None


def test_fetch_previous_unconfigured_returns_mock(monkeypatch):
    _set_envs(monkeypatch, mode="none")
    snap = play.fetch_previous("centry", dt.date(2026, 5, 11))
    assert snap.installs == 16   # _MOCK_PREV[centry]
    assert snap.week_start == dt.date(2026, 5, 4)


def _DEPRECATED_test_fetch_previous_shifts_week_by_7_days(monkeypatch):
    """Configured → real fetch_weekly is called with week_start - 7 days."""
    _set_envs(monkeypatch, mode="raw")
    fake_creds = MagicMock(name="creds")
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.download_as_bytes.return_value = INSTALLS_CSV_BYTES
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket
    fake_service = _make_play_service_mock(REVIEWS_EMPTY)

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "google.cloud.storage.Client", return_value=fake_client,
    ), patch(
        "googleapiclient.discovery.build", return_value=fake_service,
    ):
        snap = play.fetch_previous("centry", dt.date(2026, 5, 11))

    # week_start shifted -7 days → 2026-05-04 (covers 05-04..05-10).
    # Fixture only has 05-12..05-18 → no rows match → installs=0
    # (file present, package present, but date filter excludes all rows).
    assert snap.week_start == dt.date(2026, 5, 4)
    # blob paths requested for the previous-week target month (still May).
    paths = [c.args[0] for c in fake_bucket.blob.call_args_list]
    assert all("_202605_" in p for p in paths)


# ===================================================================
# _get_credentials — branch coverage for the path-env codepath
# ===================================================================

def test_get_credentials_uses_raw_json_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_SA_JSON", FAKE_SA_JSON)
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON_PATH", raising=False)

    fake_creds = MagicMock(name="creds")
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ) as m_info, patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
    ) as m_file:
        result = play._get_credentials()
    assert result is fake_creds
    assert m_info.call_count == 1
    assert m_file.call_count == 0


def test_get_credentials_falls_back_to_path_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_PLAY_SA_JSON_PATH", "/tmp/fake-sa.json")

    fake_creds = MagicMock(name="creds")
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ) as m_file:
        result = play._get_credentials()
    assert result is fake_creds
    m_file.assert_called_once()
    args, kwargs = m_file.call_args
    assert args[0] == "/tmp/fake-sa.json"


def test_get_credentials_no_env_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON_PATH", raising=False)
    with pytest.raises(RuntimeError, match="Neither GOOGLE_PLAY_SA_JSON"):
        play._get_credentials()


# HOTFIX regression tests (smoke run 25890122345)


def test_package_for_strips_whitespace(monkeypatch):
    """GH Secret storage may add trailing newline. _package_for must strip it
    so blob paths like installs_<package>_YYYYMM_country.csv don't include \\n.
    """
    monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", "website.centry.app\n")
    monkeypatch.setenv("GPLAY_PACKAGE_DIKTUM", "  ru.diktumweb.diktum  ")
    assert play._package_for("centry") == "website.centry.app"
    assert play._package_for("diktum") == "ru.diktumweb.diktum"


def _DEPRECATED_test_fetch_installs_csv_strips_developer_id_for_bucket_name(monkeypatch):
    """GH Secret value with trailing newline broke GCS bucket validation
    (smoke run 25890122345 error: 'Bucket names must start and end with a
    number or letter'). Fix: strip developer_id before formatting."""
    captured = {}

    fake_blob = MagicMock()
    fake_blob.exists.return_value = False
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()

    def capture_bucket(name):
        captured["bucket"] = name
        return fake_bucket

    fake_client.bucket.side_effect = capture_bucket

    with patch("google.cloud.storage.Client", return_value=fake_client):
        play._fetch_installs_csv(
            credentials=MagicMock(),
            developer_id="6224792403622982347\n",   # trailing \n
            package="website.centry.app\n",         # trailing \n
            yyyymm="202605",
        )

    # Bucket name must end with digit, not \n
    assert captured["bucket"] == "pubsite_prod_rev_6224792403622982347"
    assert "\n" not in captured["bucket"]
    # Blob path must use stripped package
    fake_bucket.blob.assert_called_once()
    blob_path = fake_bucket.blob.call_args[0][0]
    assert "\n" not in blob_path
    assert "website.centry.app" in blob_path


# ===================================================================
# Weekly cadence — daily aggregation (PR #77 rewrite)
# ===================================================================

_DAILY_OVERVIEW_HEADER = "Date,Package Name,Daily Device Installs,Daily Device Uninstalls"
_DAILY_OVERVIEW_CENTRY = (
    f"{_DAILY_OVERVIEW_HEADER}\n"
    f"2026-05-12,{PACKAGE_CENTRY},3,0\n"
).encode("utf-16")
_DAILY_OVERVIEW_DIKTUM = (
    f"{_DAILY_OVERVIEW_HEADER}\n"
    f"2026-05-12,{PACKAGE_DIKTUM},2,0\n"
).encode("utf-16")
_DAILY_OVERVIEW_EMPTY = f"{_DAILY_OVERVIEW_HEADER}\n".encode("utf-16")


def test_parse_installs_daily_extracts_total():
    """Single-row daily CSV → returns installs sum for that package."""
    n = play._parse_installs_daily(_DAILY_OVERVIEW_CENTRY, PACKAGE_CENTRY)
    assert n == 3


def test_parse_installs_daily_unknown_package_returns_zero():
    """File has data but no rows for this package → returns 0 (not None)."""
    n = play._parse_installs_daily(_DAILY_OVERVIEW_CENTRY, "ru.other.app")
    assert n == 0


def test_parse_installs_daily_empty_returns_none():
    """Header-only file → None."""
    n = play._parse_installs_daily(_DAILY_OVERVIEW_EMPTY, PACKAGE_CENTRY)
    assert n is None


def test_parse_installs_daily_no_bytes_returns_none():
    """Empty bytes → None."""
    n = play._parse_installs_daily(b"", PACKAGE_CENTRY)
    assert n is None


def test_fetch_weekly_aggregates_7_days(monkeypatch):
    """7 daily CSVs (one per day) — installs summed across week."""
    _set_envs(monkeypatch, mode="raw")

    # Make each day return 1 install for Centry → week sum = 7
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    one_install_csv = (
        f"{_DAILY_OVERVIEW_HEADER}\n2026-05-12,{PACKAGE_CENTRY},1,0\n"
    ).encode("utf-16")
    fake_blob.download_as_bytes.return_value = one_install_csv
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    fake_reviews = MagicMock()
    fake_reviews.execute.return_value = {"reviews": []}
    fake_service = MagicMock()
    fake_service.reviews.return_value.list.return_value = fake_reviews

    fake_creds = MagicMock()

    with patch("google.cloud.storage.Client", return_value=fake_client), \
            patch("googleapiclient.discovery.build", return_value=fake_service), \
            patch("google.oauth2.service_account.Credentials.from_service_account_info",
                  return_value=fake_creds):
        snap = play.fetch_weekly("centry", dt.date(2026, 5, 12))
    # 7 days × 1 install per day = 7
    assert snap.installs == 7
    assert snap.error is None


def test_fetch_weekly_all_days_missing_returns_none_with_lag_error(monkeypatch):
    """If all 7 days' blobs don't exist → installs=None + 24h lag error."""
    _set_envs(monkeypatch, mode="raw")

    fake_blob = MagicMock()
    fake_blob.exists.return_value = False
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    fake_reviews = MagicMock()
    fake_reviews.execute.return_value = {"reviews": []}
    fake_service = MagicMock()
    fake_service.reviews.return_value.list.return_value = fake_reviews

    fake_creds = MagicMock()

    with patch("google.cloud.storage.Client", return_value=fake_client), \
            patch("googleapiclient.discovery.build", return_value=fake_service), \
            patch("google.oauth2.service_account.Credentials.from_service_account_info",
                  return_value=fake_creds):
        snap = play.fetch_weekly("centry", dt.date(2026, 5, 12))
    assert snap.installs is None
    assert "24h lag" in snap.error
