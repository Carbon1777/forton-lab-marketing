"""Unit tests for src/store_metrics/play.py — manual CSV installs + reviews API.

After 2026-05-15 canonical pivot:
    - Installs path reads `.metrics/gplay_weekly/<YYYY-Www>.csv` —
      Play Console "Statistics → Export CSV" download (UTF-16 LE BOM).
    - Reviews path keeps androidpublisher v3 (optional, SA credentials).
    - No more GCS bucket / GPLAY_DEVELOPER_ID env.

External services replaced (reviews path only):
    - googleapiclient.discovery.build (androidpublisher)
    - google.oauth2.service_account.Credentials (creds builder)
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import play

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store_metrics"

CSV_GPLAY = FIXTURES / "gplay_weekly_2026-W20.csv"   # UTF-16 LE BOM
REVIEWS_SAMPLE = json.loads(
    (FIXTURES / "play_reviews_sample.json").read_text(encoding="utf-8")
)
REVIEWS_EMPTY = json.loads(
    (FIXTURES / "play_reviews_empty.json").read_text(encoding="utf-8")
)

PACKAGE_CENTRY = "website.centry.app"
PACKAGE_DIKTUM = "ru.diktumweb.diktum"

# Mon 2026-05-11 == ISO 2026-W20.
WEEK_W20 = dt.date(2026, 5, 11)

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

def _set_envs(monkeypatch, *, packages: bool = True, sa: bool = False) -> None:
    """Configure envs.

    packages: whether to set GPLAY_PACKAGE_* envs (needed for _is_configured).
    sa: whether to set GOOGLE_PLAY_SA_JSON (enables reviews path).
    """
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_PLAY_SA_JSON_PATH", raising=False)
    if sa:
        monkeypatch.setenv("GOOGLE_PLAY_SA_JSON", FAKE_SA_JSON)
    if packages:
        monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", PACKAGE_CENTRY)
        monkeypatch.setenv("GPLAY_PACKAGE_DIKTUM", PACKAGE_DIKTUM)
    else:
        for k in ("GPLAY_PACKAGE_CENTRY", "GPLAY_PACKAGE_DIKTUM"):
            monkeypatch.delenv(k, raising=False)


def test_is_configured_packages_set(monkeypatch):
    """SA optional — only packages required for _is_configured."""
    _set_envs(monkeypatch, packages=True, sa=False)
    assert play._is_configured() is True


def test_is_configured_missing_packages(monkeypatch):
    _set_envs(monkeypatch, packages=False, sa=True)
    assert play._is_configured() is False


def test_is_configured_partial_packages_returns_false(monkeypatch):
    """Only one of two package envs → still False."""
    _set_envs(monkeypatch, packages=False)
    monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", PACKAGE_CENTRY)
    assert play._is_configured() is False


def test_has_sa_credentials_with_raw_env(monkeypatch):
    _set_envs(monkeypatch, packages=True, sa=True)
    assert play._has_sa_credentials() is True


def test_has_sa_credentials_with_path_env(monkeypatch):
    _set_envs(monkeypatch, packages=True, sa=False)
    monkeypatch.setenv("GOOGLE_PLAY_SA_JSON_PATH", "/tmp/sa.json")
    assert play._has_sa_credentials() is True


def test_has_sa_credentials_without_any(monkeypatch):
    _set_envs(monkeypatch, packages=True, sa=False)
    assert play._has_sa_credentials() is False


def test_package_for_centry(monkeypatch):
    _set_envs(monkeypatch, packages=True)
    assert play._package_for("centry") == PACKAGE_CENTRY


def test_package_for_diktum(monkeypatch):
    _set_envs(monkeypatch, packages=True)
    assert play._package_for("diktum") == PACKAGE_DIKTUM


def test_package_for_missing_env_raises(monkeypatch):
    _set_envs(monkeypatch, packages=False)
    with pytest.raises(RuntimeError, match="GPLAY_PACKAGE_CENTRY"):
        play._package_for("centry")


def test_package_for_strips_whitespace(monkeypatch):
    """GH Secret may add trailing newline — _package_for must strip."""
    monkeypatch.setenv("GPLAY_PACKAGE_CENTRY", "website.centry.app\n")
    monkeypatch.setenv("GPLAY_PACKAGE_DIKTUM", "  ru.diktumweb.diktum  ")
    assert play._package_for("centry") == "website.centry.app"
    assert play._package_for("diktum") == "ru.diktumweb.diktum"


# ===================================================================
# _iso_week_key
# ===================================================================

def test_iso_week_key_for_mon_2026_05_11():
    assert play._iso_week_key(dt.date(2026, 5, 11)) == "2026-W20"


def test_iso_week_key_pads_week_number():
    assert play._iso_week_key(dt.date(2026, 1, 5)) == "2026-W02"


# ===================================================================
# _read_csv_installs — using real UTF-16 LE BOM fixture
# ===================================================================

def _stage_csv(
    tmp_path: Path,
    fixture: Path,
    iso_key: str = "2026-W20",
    ext: str = "csv",
) -> Path:
    """Copy a fixture CSV into a fake repo root structure."""
    target = tmp_path / ".metrics" / "gplay_weekly" / f"{iso_key}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, target)
    return target


def test_read_csv_installs_filters_package(tmp_path):
    """Mixed Centry + Diktum fixture: PACKAGE_CENTRY drops Diktum rows.

    week_start = Mon 2026-05-11 → window 05-11..05-17.
    Fixture covers 05-12..05-18; Centry rows in window 05-12..05-17:
    3 + 2 + 1 + 2 + 1 + 1 = 10. (05-18 outside window.)
    """
    _stage_csv(tmp_path, CSV_GPLAY)
    total, by_cc = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 10
    assert sum(by_cc.values()) == 10
    # Diktum-only countries should not leak in.
    total_d, by_d = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_DIKTUM)
    # Diktum in window 05-12..05-17: 2 + 1 = 3 (05-14 has 0 installs).
    assert total_d == 3
    assert by_d == {"RU": 3}


def test_read_csv_installs_utf16_bom_decoded_correctly(tmp_path):
    """Verify UTF-16 LE BOM file is decoded — fixture starts with 0xFF 0xFE."""
    _stage_csv(tmp_path, CSV_GPLAY)
    # Sanity: фикстура действительно UTF-16 LE BOM
    raw = CSV_GPLAY.read_bytes()
    assert raw[:2] == b"\xff\xfe"
    total, _ = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 10


def test_read_csv_installs_groups_by_country(tmp_path):
    """Centry rows in W20 window grouped by country."""
    _stage_csv(tmp_path, CSV_GPLAY)
    total, by_cc = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 10
    # Fixture distribution (rows 05-12..05-17): mostly RU with KZ/BY.
    assert "RU" in by_cc
    assert sum(by_cc.values()) == total


def test_read_csv_installs_missing_file_returns_none(tmp_path):
    """No file → (None, {})."""
    total, by_cc = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total is None
    assert by_cc == {}


def test_read_csv_installs_unknown_package_returns_zero(tmp_path):
    """File present, package not in any row → (0, {}) because other packages
    matched_any_row=True (file has data, just not this app)."""
    _stage_csv(tmp_path, CSV_GPLAY)
    total, by_cc = play._read_csv_installs(
        tmp_path, WEEK_W20, "com.nonexistent.app",
    )
    assert total == 0
    assert by_cc == {}


def test_read_csv_installs_case_insensitive_package_match(tmp_path):
    """Package match is case-insensitive (defensive)."""
    target = tmp_path / ".metrics" / "gplay_weekly" / "2026-W20.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "Date,Package Name,Country,Daily Device Installs,Daily Device Uninstalls\n"
        "2026-05-12,Website.Centry.App,RU,5,0\n",   # mixed case
        encoding="utf-8",
    )
    total, by_cc = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 5
    assert by_cc == {"RU": 5}


def test_read_csv_installs_filters_date_range(tmp_path):
    """Only dates inside week_start..week_end+6 counted."""
    _stage_csv(tmp_path, CSV_GPLAY)
    # Same fixture, narrower window → only 05-12 row for Centry.
    total, _ = play._read_csv_installs(
        tmp_path, dt.date(2026, 5, 12), PACKAGE_CENTRY,
    )
    # week_start=2026-05-12 → window 05-12..05-18 (all 7 days)
    # Centry rows in fixture: 3+2+1+2+1+1+1 = 11
    assert total == 11


def test_read_csv_installs_txt_extension_accepted(tmp_path):
    """User may save the file as .txt."""
    _stage_csv(tmp_path, CSV_GPLAY, ext="txt")
    total, _ = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 10


def test_read_csv_installs_invalid_installs_value_skipped(tmp_path):
    """Non-int Daily Device Installs → skip row."""
    target = tmp_path / ".metrics" / "gplay_weekly" / "2026-W20.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "Date,Package Name,Country,Daily Device Installs,Daily Device Uninstalls\n"
        "2026-05-12,website.centry.app,RU,abc,0\n"
        "2026-05-13,website.centry.app,RU,5,0\n",
        encoding="utf-8",
    )
    total, _ = play._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 5


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
# _get_credentials — branch coverage
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


# ===================================================================
# _fetch_reviews — mock androidpublisher
# ===================================================================

def _make_play_service_mock(*page_responses) -> MagicMock:
    """Build a mocked googleapiclient service."""
    service = MagicMock()
    reviews_obj = MagicMock()
    list_mock = MagicMock()
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
    """First page returns nextPageToken → second page fetched + merged."""
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
            {"comments": [{"userComment": {"starRating": "5"}}]},
            {"comments": [{"userComment": {"starRating": None}}]},
            {"comments": [{"userComment": {"starRating": 9}}]},
            {"comments": [{"userComment": {"starRating": "abc"}}]},
            {"comments": [{"userComment": {"starRating": 4}}]},
            {"comments": []},
            {},
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
    """Without package envs → mock StoreSnapshot, no HTTP calls."""
    _set_envs(monkeypatch, packages=False)
    snap = play.fetch_weekly("centry", WEEK_W20)
    assert snap.product == "centry"
    assert snap.store == "google_play"
    assert snap.installs == 11   # _MOCK_INSTALLS
    assert snap.rating == 4.6
    assert snap.top_country == "RU"


def test_fetch_weekly_csv_missing_soft_fallback(monkeypatch, tmp_path):
    """Configured but CSV not present → installs=None, error message set."""
    _set_envs(monkeypatch, packages=True, sa=False)
    with patch.object(play, "_repo_root", return_value=tmp_path):
        snap = play.fetch_weekly("centry", WEEK_W20)
    assert snap.installs is None
    assert snap.error is not None
    assert "GPlay CSV не положен" in snap.error
    # No SA configured → rating=None (no API call).
    assert snap.rating is None


def test_fetch_weekly_csv_ok_no_sa_returns_installs_no_rating(
    monkeypatch, tmp_path,
):
    """CSV present, no SA credentials → installs from CSV, rating=None."""
    _set_envs(monkeypatch, packages=True, sa=False)
    _stage_csv(tmp_path, CSV_GPLAY)
    with patch.object(play, "_repo_root", return_value=tmp_path):
        snap = play.fetch_weekly("centry", WEEK_W20)
    assert snap.installs == 10
    assert snap.rating is None
    assert snap.error is None


def test_fetch_weekly_csv_ok_sa_ok_full_snapshot(monkeypatch, tmp_path):
    """Both CSV + SA work → installs + rating + top_country, no error."""
    _set_envs(monkeypatch, packages=True, sa=True)
    _stage_csv(tmp_path, CSV_GPLAY)

    fake_creds = MagicMock(name="creds")
    fake_service = _make_play_service_mock(REVIEWS_SAMPLE)

    with patch.object(
        play, "_repo_root", return_value=tmp_path,
    ), patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "googleapiclient.discovery.build", return_value=fake_service,
    ):
        snap = play.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 10
    assert snap.top_country == "RU"
    assert snap.rating == pytest.approx(4.5)
    assert snap.error is None


def test_fetch_weekly_reviews_failure_does_not_break_installs(
    monkeypatch, tmp_path,
):
    """androidpublisher raises → installs still returned, rating=None."""
    _set_envs(monkeypatch, packages=True, sa=True)
    _stage_csv(tmp_path, CSV_GPLAY)

    fake_creds = MagicMock(name="creds")

    def raise_review(*args, **kwargs):
        raise RuntimeError("androidpublisher build failed")

    with patch.object(
        play, "_repo_root", return_value=tmp_path,
    ), patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ), patch(
        "googleapiclient.discovery.build", side_effect=raise_review,
    ):
        snap = play.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 10
    assert snap.rating is None
    assert snap.error is None


def test_fetch_weekly_for_diktum_isolates_correctly(monkeypatch, tmp_path):
    """Same CSV, requesting Diktum → only Diktum rows summed."""
    _set_envs(monkeypatch, packages=True, sa=False)
    _stage_csv(tmp_path, CSV_GPLAY)
    with patch.object(play, "_repo_root", return_value=tmp_path):
        snap = play.fetch_weekly("diktum", WEEK_W20)
    # Diktum rows 05-12..05-17: 2 + 1 = 3 (05-14 has 0)
    assert snap.installs == 3
    assert snap.top_country == "RU"
    assert snap.error is None


# ===================================================================
# fetch_previous
# ===================================================================

def test_fetch_previous_unconfigured_returns_mock(monkeypatch):
    _set_envs(monkeypatch, packages=False)
    snap = play.fetch_previous("centry", WEEK_W20)
    assert snap.installs == 16   # _MOCK_PREV[centry]
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_previous_shifts_week_by_7_days(monkeypatch, tmp_path):
    """Configured → calls fetch_weekly with week_start - 7 days.
    W19 CSV absent in fixture → installs=None soft-fallback."""
    _set_envs(monkeypatch, packages=True, sa=False)
    with patch.object(play, "_repo_root", return_value=tmp_path):
        snap = play.fetch_previous("centry", WEEK_W20)
    assert snap.week_start == dt.date(2026, 5, 4)
    assert snap.installs is None
    assert snap.error is not None
    assert "GPlay CSV не положен" in snap.error
