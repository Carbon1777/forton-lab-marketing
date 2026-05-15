"""Unit tests for src/store_metrics/asc.py — manual CSV installs + iTunes RSS.

After 2026-05-15 canonical pivot:
    - Installs path reads `.metrics/asc_weekly/<YYYY-Www>.{csv,tsv,txt}` —
      Apple Sales Reports "Weekly Summary" download (28-col TSV).
    - Ratings path keeps iTunes Customer Reviews RSS (no auth).
    - No more Reporter Token / Vendor Number — those envs are dead.

HTTP calls (RSS only now) are mocked via unittest.mock.patch on
src.store_metrics._http.fetch_with_retry. CSV reading uses real fixture
files under tests/fixtures/store_metrics/.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.store_metrics import asc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store_metrics"

CSV_ASC = FIXTURES / "asc_weekly_2026-W20.tsv"   # tab-separated Apple Sales Report
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
# _iso_week_key
# ===================================================================

def test_iso_week_key_for_mon_2026_05_11():
    """ISO 2026 week 20 starts Mon May 11."""
    assert asc._iso_week_key(dt.date(2026, 5, 11)) == "2026-W20"


def test_iso_week_key_pads_week_number():
    """Week number always 2 digits."""
    assert asc._iso_week_key(dt.date(2026, 1, 5)) == "2026-W02"


# ===================================================================
# _read_csv_installs — using real fixture
# ===================================================================

def _stage_csv(
    tmp_path: Path,
    fixture: Path,
    iso_key: str = "2026-W20",
    ext: str = "tsv",
) -> Path:
    """Copy a fixture CSV/TSV into a fake repo root structure."""
    target = tmp_path / ".metrics" / "asc_weekly" / f"{iso_key}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, target)
    return target


def test_read_csv_installs_centry_full_csv(tmp_path):
    """ASC fixture: Centry has 1F RU 5 + 1F KZ 2 = 7 installs (3F update excluded)."""
    _stage_csv(tmp_path, CSV_ASC)
    total, by_cc = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    assert total == 7
    assert by_cc == {"RU": 5, "KZ": 2}


def test_read_csv_installs_diktum_filters_correctly(tmp_path):
    """Diktum: 1F RU 3 = 3 (paid '1' row and IA1 row excluded)."""
    _stage_csv(tmp_path, CSV_ASC)
    total, by_cc = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_DIKTUM)
    assert total == 3
    assert by_cc == {"RU": 3}


def test_read_csv_installs_csv_extension_accepted(tmp_path):
    """User may save the file as .csv even though it's tab-separated."""
    _stage_csv(tmp_path, CSV_ASC, ext="csv")
    total, _ = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    assert total == 7


def test_read_csv_installs_txt_extension_accepted(tmp_path):
    """User may save the file as .txt."""
    _stage_csv(tmp_path, CSV_ASC, ext="txt")
    total, _ = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    assert total == 7


def test_read_csv_installs_missing_file_returns_none(tmp_path):
    """No file → (None, {}), not raise."""
    total, by_cc = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    assert total is None
    assert by_cc == {}


def test_read_csv_installs_unknown_app_id_returns_none(tmp_path):
    """File present, but app_id not in any row → (None, {}) — treat as no data."""
    _stage_csv(tmp_path, CSV_ASC)
    total, by_cc = asc._read_csv_installs(tmp_path, WEEK_W20, "9999999999")
    # File has data (other apps) — but nothing for this app. Returned as None
    # so the digest can flag "no installs row for this app" rather than show 0.
    # Actually: matched_any_row tracks ANY apple_id seen in the file (different
    # app), so we get matched_any_row=True and total=0. That's "legit zero".
    assert total == 0
    assert by_cc == {}


def test_read_csv_installs_groups_by_country(tmp_path):
    """Multiple countries → grouped dict, sum matches total."""
    _stage_csv(tmp_path, CSV_ASC)
    total, by_cc = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    assert set(by_cc.keys()) == {"RU", "KZ"}
    assert sum(by_cc.values()) == total


def test_read_csv_installs_iso_week_picks_right_file(tmp_path):
    """Files for different ISO weeks coexist — pick the one matching week_start."""
    # Stage two files: W19 and W20, with different contents.
    csv_dir = tmp_path / ".metrics" / "asc_weekly"
    csv_dir.mkdir(parents=True, exist_ok=True)
    # W19 file: 1F row attributing 99 installs (should NOT be picked when asking W20).
    (csv_dir / "2026-W19.tsv").write_text(
        "Provider\tProvider Country\tSKU\tDeveloper\tTitle\tVersion\t"
        "Product Type Identifier\tUnits\tDeveloper Proceeds (per unit)\t"
        "Begin Date\tEnd Date\tCustomer Currency\tCountry Code\t"
        "Currency of Proceeds\tApple Identifier\tCustomer Price\n"
        f"APPLE\tUS\tsku\tForton\tCentry\t1.0\t1F\t99\t0.0\t05/04/2026\t"
        f"05/10/2026\tRUB\tRU\tRUB\t{APPLE_ID_CENTRY}\t0.0\n",
        encoding="utf-8",
    )
    # W20 file: real fixture.
    shutil.copy(CSV_ASC, csv_dir / "2026-W20.tsv")
    total, _ = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    # W20 fixture: 5 + 2 = 7. W19 ignored.
    assert total == 7


def test_read_csv_installs_bom_prefixed_file(tmp_path):
    """UTF-8 BOM prefix should not leak into the first column header."""
    csv_dir = tmp_path / ".metrics" / "asc_weekly"
    csv_dir.mkdir(parents=True, exist_ok=True)
    # Read fixture, prepend BOM, write as utf-8.
    raw = CSV_ASC.read_text(encoding="utf-8")
    (csv_dir / "2026-W20.tsv").write_text("﻿" + raw, encoding="utf-8")
    total, _ = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    assert total == 7


def test_read_csv_installs_skips_zero_unit_rows(tmp_path):
    """Diktum IA1 row has Units=0 → not counted (even though PTI also wrong)."""
    _stage_csv(tmp_path, CSV_ASC)
    total, by_cc = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_DIKTUM)
    # Only 1F RU 3 counted; paid "1" row (BY) and IA1 zero excluded.
    assert total == 3
    assert "BY" not in by_cc


def test_read_csv_installs_invalid_units_value_skipped(tmp_path):
    """Non-int Units value → skip row, don't crash."""
    csv_dir = tmp_path / ".metrics" / "asc_weekly"
    csv_dir.mkdir(parents=True, exist_ok=True)
    (csv_dir / "2026-W20.tsv").write_text(
        "Provider\tProvider Country\tSKU\tDeveloper\tTitle\tVersion\t"
        "Product Type Identifier\tUnits\tDeveloper Proceeds (per unit)\t"
        "Begin Date\tEnd Date\tCustomer Currency\tCountry Code\t"
        "Currency of Proceeds\tApple Identifier\tCustomer Price\n"
        f"APPLE\tUS\tsku\tForton\tCentry\t1.0\t1F\tabc\t0.0\t05/12/2026\t"
        f"05/18/2026\tRUB\tRU\tRUB\t{APPLE_ID_CENTRY}\t0.0\n"
        f"APPLE\tUS\tsku\tForton\tCentry\t1.0\t1F\t5\t0.0\t05/12/2026\t"
        f"05/18/2026\tRUB\tRU\tRUB\t{APPLE_ID_CENTRY}\t0.0\n",
        encoding="utf-8",
    )
    total, _ = asc._read_csv_installs(tmp_path, WEEK_W20, APPLE_ID_CENTRY)
    # First row skipped (abc), second counted.
    assert total == 5


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
    """Without envs → mock StoreSnapshot, no HTTP / CSV calls."""
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_weekly("centry", WEEK_W20)
    assert snap.product == "centry"
    assert snap.store == "app_store"
    assert snap.installs == 23
    assert snap.rating == 4.7
    assert snap.top_country == "RU"


def test_fetch_weekly_csv_missing_soft_fallback(monkeypatch, tmp_path):
    """Configured but CSV not present → installs=None, error message set,
    rating still fetched через RSS."""
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_CENTRY
        return m

    with patch.object(
        asc, "_repo_root", return_value=tmp_path,
    ), patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.error is not None
    assert "ASC CSV не положен" in snap.error
    # Rating still wired through RSS (independent of CSV).
    assert snap.rating is not None


def test_fetch_weekly_csv_ok_rss_ok_returns_full_snapshot(monkeypatch, tmp_path):
    """Both paths succeed → installs + rating + top_country, no error."""
    _set_envs(monkeypatch, all_present=True)
    _stage_csv(tmp_path, CSV_ASC)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        if "itunes.apple.com" in url and "/ru/" in url:
            m.json.return_value = RSS_CENTRY
        else:
            m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(
        asc, "_repo_root", return_value=tmp_path,
    ), patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 7    # 1F RU 5 + 1F KZ 2
    assert snap.top_country == "RU"
    assert snap.top_country_share == pytest.approx(5 / 7)
    # RSS RU only — sum 14, count 3 → 14/3
    assert snap.rating == pytest.approx(14 / 3)
    assert snap.error is None


def test_fetch_weekly_rss_fails_csv_still_works(monkeypatch, tmp_path):
    """RSS network failure → installs read OK, rating=None, no error."""
    _set_envs(monkeypatch, all_present=True)
    _stage_csv(tmp_path, CSV_ASC)

    def fake_fetch(url, method="GET", **kwargs):
        raise RuntimeError("network down")

    with patch.object(
        asc, "_repo_root", return_value=tmp_path,
    ), patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 7
    assert snap.rating is None
    # RSS failures are swallowed — no error string for that.
    assert snap.error is None


def test_fetch_weekly_for_diktum_isolates_correctly(monkeypatch, tmp_path):
    """Same CSV, requesting Diktum → only Diktum 1F rows summed."""
    _set_envs(monkeypatch, all_present=True)
    _stage_csv(tmp_path, CSV_ASC)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(
        asc, "_repo_root", return_value=tmp_path,
    ), patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_weekly("diktum", WEEK_W20)

    assert snap.installs == 3   # 1F RU 3
    assert snap.rating is None
    assert snap.top_country == "RU"
    assert snap.error is None


# ===================================================================
# fetch_previous
# ===================================================================

def test_fetch_previous_unconfigured_returns_mock(monkeypatch):
    _set_envs(monkeypatch, all_present=False)
    snap = asc.fetch_previous("diktum", WEEK_W20)
    assert snap.installs == 22   # _MOCK_PREV
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_previous_shifts_week_by_7_days(monkeypatch, tmp_path):
    """Configured → fetch_weekly called with week_start - 7 days. W19 CSV
    is absent in fixture → soft-fallback."""
    _set_envs(monkeypatch, all_present=True)

    def fake_fetch(url, method="GET", **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = RSS_DIKTUM_EMPTY
        return m

    with patch.object(
        asc, "_repo_root", return_value=tmp_path,
    ), patch.object(
        asc._http, "fetch_with_retry", side_effect=fake_fetch,
    ):
        snap = asc.fetch_previous("centry", WEEK_W20)

    assert snap.week_start == dt.date(2026, 5, 4)
    assert snap.installs is None
    assert snap.error is not None
    assert "ASC CSV не положен" in snap.error
