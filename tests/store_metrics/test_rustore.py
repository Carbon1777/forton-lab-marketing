"""Unit tests for src/store_metrics/rustore.py — JWS auth + reviews + CSV.

HTTP calls mocked via unittest.mock.patch on src.store_metrics._http.fetch_with_retry.
RSA test key generated once for the module (fast — 2048-bit, ~50ms).
CSV reading uses real fixture files under tests/fixtures/store_metrics/.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.store_metrics import rustore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "store_metrics"

AUTH_RESPONSE = json.loads(
    (FIXTURES / "rustore_auth_response.json").read_text(encoding="utf-8")
)
REVIEWS_CENTRY = json.loads(
    (FIXTURES / "rustore_reviews_centry.json").read_text(encoding="utf-8")
)
REVIEWS_EMPTY = json.loads(
    (FIXTURES / "rustore_reviews_empty.json").read_text(encoding="utf-8")
)

CSV_FULL = FIXTURES / "rustore_weekly_2026-W20_full.csv"
CSV_MINIMAL = FIXTURES / "rustore_weekly_2026-W20_minimal.csv"
CSV_RUSSIAN = FIXTURES / "rustore_weekly_2026-W20_russian.csv"
CSV_BOM = FIXTURES / "rustore_weekly_2026-W20_bom.csv"

PACKAGE_CENTRY = "website.centry.app"
PACKAGE_DIKTUM = "ru.diktumweb.diktum"
KEY_ID = "2351028465"
COMPANY_ID = "2351526569"

# Mon 2026-05-11 == ISO 2026-W20.
WEEK_W20 = dt.date(2026, 5, 11)


# ===================================================================
# Test RSA key (generated once per test session)
# ===================================================================

@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, object]:
    """Generate 2048-bit RSA keypair, return (pem_bytes, public_key)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private_key.public_key()


# ===================================================================
# env / configuration
# ===================================================================

def _set_envs(monkeypatch, rsa_pem: bytes, *, mode: str = "raw") -> None:
    """Configure all envs. mode in {raw, path, none}."""
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY_PATH", raising=False)
    if mode == "raw":
        monkeypatch.setenv("RUSTORE_PRIVATE_KEY", rsa_pem.decode("utf-8"))
    # path mode handled by individual tests (tmp_path needed)
    if mode != "none":
        monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
        monkeypatch.setenv("RUSTORE_COMPANY_ID", COMPANY_ID)
        monkeypatch.setenv("RUSTORE_PACKAGE_CENTRY", PACKAGE_CENTRY)
        monkeypatch.setenv("RUSTORE_PACKAGE_DIKTUM", PACKAGE_DIKTUM)
    else:
        for k in (
            "RUSTORE_KEY_ID", "RUSTORE_COMPANY_ID",
            "RUSTORE_PACKAGE_CENTRY", "RUSTORE_PACKAGE_DIKTUM",
        ):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Ensure token cache state does not leak between tests."""
    rustore._reset_token_cache()
    yield
    rustore._reset_token_cache()


def test_is_configured_all_envs_set(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    assert rustore._is_configured() is True


def test_is_configured_path_alternative(monkeypatch, rsa_keypair, tmp_path):
    """Path env can substitute for raw PEM env."""
    pem, _ = rsa_keypair
    pem_file = tmp_path / "rustore_private.pem"
    pem_file.write_bytes(pem)
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY_PATH", str(pem_file))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
    monkeypatch.setenv("RUSTORE_COMPANY_ID", COMPANY_ID)
    monkeypatch.setenv("RUSTORE_PACKAGE_CENTRY", PACKAGE_CENTRY)
    monkeypatch.setenv("RUSTORE_PACKAGE_DIKTUM", PACKAGE_DIKTUM)
    assert rustore._is_configured() is True


def test_is_configured_missing_envs(monkeypatch, rsa_keypair):
    """None of the envs set → False."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="none")
    assert rustore._is_configured() is False


def test_is_configured_empty_string_counts_as_missing(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    monkeypatch.setenv("RUSTORE_KEY_ID", "")
    assert rustore._is_configured() is False


def test_package_for_centry(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    assert rustore._package_for("centry") == PACKAGE_CENTRY


def test_package_for_diktum(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    assert rustore._package_for("diktum") == PACKAGE_DIKTUM


def test_package_for_missing_env_raises(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="none")
    with pytest.raises(RuntimeError, match="RUSTORE_PACKAGE_CENTRY"):
        rustore._package_for("centry")


# ===================================================================
# _load_private_key
# ===================================================================

def test_load_private_key_from_raw_env(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    key = rustore._load_private_key()
    # Has the right interface for signing
    assert hasattr(key, "sign")


def test_load_private_key_from_path(monkeypatch, rsa_keypair, tmp_path):
    pem, _ = rsa_keypair
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY", raising=False)
    pem_file = tmp_path / "rs.pem"
    pem_file.write_bytes(pem)
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY_PATH", str(pem_file))
    key = rustore._load_private_key()
    assert hasattr(key, "sign")


def test_load_private_key_invalid_pem_raises_runtimeerror(monkeypatch):
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", "this is not a PEM key at all")
    with pytest.raises(RuntimeError, match="Failed to parse"):
        rustore._load_private_key()


def test_load_private_key_no_env_raises_runtimeerror(monkeypatch):
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(RuntimeError, match="Neither"):
        rustore._load_private_key()


def test_load_private_key_path_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("RUSTORE_PRIVATE_KEY", raising=False)
    monkeypatch.setenv(
        "RUSTORE_PRIVATE_KEY_PATH", str(tmp_path / "nonexistent.pem")
    )
    with pytest.raises(RuntimeError, match="Failed to read"):
        rustore._load_private_key()


# ===================================================================
# _sign_jws
# ===================================================================

def test_sign_jws_produces_valid_signature(rsa_keypair):
    """Signature verifies with the matching public key (RSA-SHA512 PKCS1v15)."""
    pem, public_key = rsa_keypair
    private_key = serialization.load_pem_private_key(pem, password=None)
    timestamp = "2026-05-14T19:31:17.580+03:00"
    sig_b64 = rustore._sign_jws(KEY_ID, timestamp, private_key)
    sig_bytes = base64.b64decode(sig_b64)
    # Public key verification raises InvalidSignature on mismatch.
    public_key.verify(
        sig_bytes,
        (KEY_ID + timestamp).encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )


def test_sign_jws_is_base64_not_hex(rsa_keypair):
    """Signature must be base64 (RESEARCH §5), not hex."""
    pem, _ = rsa_keypair
    private_key = serialization.load_pem_private_key(pem, password=None)
    sig = rustore._sign_jws(KEY_ID, "2026-05-14T00:00:00.000+03:00", private_key)
    # Base64 length for 256-byte RSA-2048 signature = 344 chars, hex would be 512.
    assert len(sig) == 344
    # Must decode cleanly as base64.
    decoded = base64.b64decode(sig)
    assert len(decoded) == 256   # RSA-2048 → 256-byte signature


def test_sign_jws_deterministic_for_same_input(rsa_keypair):
    """RSA-PKCS1v15 is deterministic — same input twice → same signature."""
    pem, _ = rsa_keypair
    private_key = serialization.load_pem_private_key(pem, password=None)
    timestamp = "2026-05-14T19:31:17.580+03:00"
    sig1 = rustore._sign_jws(KEY_ID, timestamp, private_key)
    sig2 = rustore._sign_jws(KEY_ID, timestamp, private_key)
    assert sig1 == sig2


# ===================================================================
# _authenticate
# ===================================================================

def _mock_response(json_body: dict, status: int = 200) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.text = json.dumps(json_body)
    return resp


def test_authenticate_constructs_correct_body(monkeypatch, rsa_keypair):
    """Verify POST body fields: keyId, timestamp, base64 signature."""
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)

    captured: dict = {}

    def fake_fetch(*, url, method, headers, json_body, **kwargs):
        captured["url"] = url
        captured["method"] = method
        captured["headers"] = headers
        captured["json_body"] = json_body
        return _mock_response(AUTH_RESPONSE)

    with patch.object(rustore._http, "fetch_with_retry", side_effect=fake_fetch):
        rustore._authenticate()

    assert captured["url"] == "https://public-api.rustore.ru/public/auth/"
    assert captured["method"] == "POST"
    assert captured["headers"]["Content-Type"] == "application/json"
    body = captured["json_body"]
    assert body["keyId"] == KEY_ID
    assert "timestamp" in body and body["timestamp"].endswith("+03:00")
    # Verify base64 signature roundtrips to 256 bytes (RSA-2048).
    sig_bytes = base64.b64decode(body["signature"])
    assert len(sig_bytes) == 256


def test_authenticate_extracts_jwe_field_from_response(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)

    with patch.object(
        rustore._http, "fetch_with_retry",
        return_value=_mock_response(AUTH_RESPONSE),
    ):
        token = rustore._authenticate()
    assert token == AUTH_RESPONSE["body"]["jwe"]


def test_authenticate_handles_code_not_ok_error(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
    bad_resp = {"code": "INVALID_KEY", "body": None, "message": "bad signature"}
    with patch.object(
        rustore._http, "fetch_with_retry",
        return_value=_mock_response(bad_resp),
    ):
        with pytest.raises(RuntimeError, match="RuStore auth failed"):
            rustore._authenticate()


def test_authenticate_handles_4xx_http_error(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
    with patch.object(
        rustore._http, "fetch_with_retry",
        return_value=_mock_response({"err": "unauthorized"}, status=401),
    ):
        with pytest.raises(RuntimeError, match="RuStore auth HTTP 401"):
            rustore._authenticate()


def test_authenticate_handles_missing_jwe_field(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
    weird_resp = {"code": "OK", "body": {"not_jwe": "x"}}
    with patch.object(
        rustore._http, "fetch_with_retry",
        return_value=_mock_response(weird_resp),
    ):
        with pytest.raises(RuntimeError, match="body.jwe missing"):
            rustore._authenticate()


# ===================================================================
# _cached_token
# ===================================================================

def test_cached_token_returns_same_token_within_ttl(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
    fetch_mock = MagicMock(return_value=_mock_response(AUTH_RESPONSE))
    with patch.object(rustore._http, "fetch_with_retry", fetch_mock):
        tok1 = rustore._cached_token()
        tok2 = rustore._cached_token()
    assert tok1 == tok2
    # Only one HTTP auth call — second invocation hit the cache.
    assert fetch_mock.call_count == 1


def test_cached_token_refetches_after_expiry(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("RUSTORE_KEY_ID", KEY_ID)
    # Make second auth return a *different* token.
    resp1 = _mock_response({"code": "OK", "body": {"jwe": "tok1", "ttl": 900}})
    resp2 = _mock_response({"code": "OK", "body": {"jwe": "tok2", "ttl": 900}})
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=[resp1, resp2],
    ):
        tok1 = rustore._cached_token()
        # Force-expire cache.
        rustore._TOKEN_CACHE["expires_at"] = dt.datetime.now(
            tz=dt.timezone.utc,
        ) - dt.timedelta(seconds=10)
        tok2 = rustore._cached_token()
    assert tok1 == "tok1"
    assert tok2 == "tok2"


# ===================================================================
# _fetch_reviews
# ===================================================================

def test_fetch_reviews_aggregates_avg_rating():
    """Centry fixture: 2 reviews (5, 4) → avg 4.5, count 2."""
    page_resp = _mock_response(REVIEWS_CENTRY)
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=page_resp,
    ):
        avg, count = rustore._fetch_reviews("bearer-xxx", PACKAGE_CENTRY)
    assert count == 2
    assert avg == pytest.approx(4.5)


def test_fetch_reviews_empty_returns_none():
    """Empty fixture → (None, 0)."""
    page_resp = _mock_response(REVIEWS_EMPTY)
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=page_resp,
    ):
        avg, count = rustore._fetch_reviews("bearer-xxx", PACKAGE_CENTRY)
    assert avg is None
    assert count == 0


def test_fetch_reviews_pagination():
    """Page 0 with last=False, page 1 with content + last=True → 4 reviews."""
    page0 = _mock_response({
        "code": "OK",
        "body": {
            "content": [
                {"appRating": 5, "commentStatus": "PUBLISHED"},
                {"appRating": 4, "commentStatus": "PUBLISHED"},
            ],
            "last": False,
        },
    })
    page1 = _mock_response({
        "code": "OK",
        "body": {
            "content": [
                {"appRating": 3, "commentStatus": "PUBLISHED"},
                {"appRating": 5, "commentStatus": "PUBLISHED"},
            ],
            "last": True,
        },
    })
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=[page0, page1],
    ):
        avg, count = rustore._fetch_reviews("bearer", PACKAGE_CENTRY)
    assert count == 4
    assert avg == pytest.approx((5 + 4 + 3 + 5) / 4)


def test_fetch_reviews_filters_non_published():
    """REJECTED / HIDDEN reviews dropped, PUBLISHED kept."""
    page = _mock_response({
        "code": "OK",
        "body": {
            "content": [
                {"appRating": 5, "commentStatus": "PUBLISHED"},
                {"appRating": 1, "commentStatus": "REJECTED"},   # excluded
                {"appRating": 2, "commentStatus": "HIDDEN"},      # excluded
                {"appRating": 3, "commentStatus": "PUBLISHED"},
            ],
            "last": True,
        },
    })
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=page,
    ):
        avg, count = rustore._fetch_reviews("bearer", PACKAGE_CENTRY)
    assert count == 2
    assert avg == pytest.approx(4.0)


def test_fetch_reviews_skips_invalid_app_rating():
    """Out-of-range / non-int ratings ignored."""
    page = _mock_response({
        "code": "OK",
        "body": {
            "content": [
                {"appRating": "5", "commentStatus": "PUBLISHED"},   # coerce OK
                {"appRating": None, "commentStatus": "PUBLISHED"},   # skip
                {"appRating": 9, "commentStatus": "PUBLISHED"},      # out of range
                {"appRating": "abc", "commentStatus": "PUBLISHED"},  # invalid
                {"appRating": 4, "commentStatus": "PUBLISHED"},      # OK
            ],
            "last": True,
        },
    })
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=page,
    ):
        avg, count = rustore._fetch_reviews("bearer", PACKAGE_CENTRY)
    assert count == 2
    assert avg == pytest.approx(4.5)


def test_fetch_reviews_http_error_stops_pagination():
    """HTTP 4xx on page 1 → stop pagination, return page 0 totals."""
    page0 = _mock_response({
        "code": "OK",
        "body": {
            "content": [{"appRating": 5, "commentStatus": "PUBLISHED"}],
            "last": False,
        },
    })
    page1 = _mock_response({}, status=403)
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=[page0, page1],
    ):
        avg, count = rustore._fetch_reviews("bearer", PACKAGE_CENTRY)
    # Only page 0 counted.
    assert count == 1
    assert avg == pytest.approx(5.0)


def test_fetch_reviews_page_cap_safety():
    """Loop caps at _REVIEWS_PAGE_CAP pages even if last is never True."""
    # Every page reports last=False with one review.
    page = _mock_response({
        "code": "OK",
        "body": {
            "content": [{"appRating": 5, "commentStatus": "PUBLISHED"}],
            "last": False,
        },
    })
    # supply enough pages; fetch_with_retry will be called up to PAGE_CAP times
    pages = [page] * (rustore._REVIEWS_PAGE_CAP + 2)
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=pages,
    ) as fetch_mock:
        avg, count = rustore._fetch_reviews("bearer", PACKAGE_CENTRY)
    assert fetch_mock.call_count == rustore._REVIEWS_PAGE_CAP
    assert count == rustore._REVIEWS_PAGE_CAP


# ===================================================================
# _iso_week_key
# ===================================================================

def test_iso_week_key_for_mon_2026_05_11():
    """ISO 2026 week 20 starts Mon May 11."""
    assert rustore._iso_week_key(dt.date(2026, 5, 11)) == "2026-W20"


def test_iso_week_key_pads_week_number():
    """Week number always 2 digits."""
    # Mon 2026-01-05 → 2026-W02
    assert rustore._iso_week_key(dt.date(2026, 1, 5)) == "2026-W02"


# ===================================================================
# _resolve_header_map
# ===================================================================

def test_resolve_header_map_english_full():
    mapping = rustore._resolve_header_map(["App", "Installs", "Country"])
    assert mapping == {"app": "App", "installs": "Installs", "country": "Country"}


def test_resolve_header_map_russian_full():
    mapping = rustore._resolve_header_map(["Приложение", "Установки", "Страна"])
    assert mapping == {
        "app": "Приложение",
        "installs": "Установки",
        "country": "Страна",
    }


def test_resolve_header_map_missing_country_returns_partial():
    mapping = rustore._resolve_header_map(["App", "Installs"])
    assert mapping == {"app": "App", "installs": "Installs"}


def test_resolve_header_map_missing_app_returns_none():
    """No app/package column → required header absent → None."""
    assert rustore._resolve_header_map(["Foo", "Installs"]) is None


def test_resolve_header_map_missing_installs_returns_none():
    assert rustore._resolve_header_map(["App", "Foo"]) is None


def test_resolve_header_map_empty_fieldnames_returns_none():
    assert rustore._resolve_header_map(None) is None
    assert rustore._resolve_header_map([]) is None


# ===================================================================
# _read_csv_installs — using real fixtures
# ===================================================================

def _stage_csv(tmp_path: Path, fixture: Path, iso_key: str = "2026-W20") -> Path:
    """Copy a fixture CSV into a fake repo root structure."""
    target = tmp_path / ".metrics" / "rustore_weekly" / f"{iso_key}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, target)
    return target


def test_read_csv_installs_full_csv(tmp_path):
    """Full CSV: Centry gets 3+1=4 installs, RU+KZ."""
    _stage_csv(tmp_path, CSV_FULL)
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 4
    assert by_cc == {"RU": 3, "KZ": 1}


def test_read_csv_installs_full_csv_diktum(tmp_path):
    """Same fixture, package=Diktum → 2 installs in RU."""
    _stage_csv(tmp_path, CSV_FULL)
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_DIKTUM)
    assert total == 2
    assert by_cc == {"RU": 2}


def test_read_csv_installs_minimal_csv(tmp_path):
    """Minimal: only App+Installs columns → installs OK, by_country empty."""
    _stage_csv(tmp_path, CSV_MINIMAL)
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 4
    assert by_cc == {}


def test_read_csv_installs_russian_headers(tmp_path):
    """Cyrillic headers should be recognized."""
    _stage_csv(tmp_path, CSV_RUSSIAN)
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 3
    assert by_cc == {"RU": 3}


def test_read_csv_installs_bom_csv(tmp_path):
    """UTF-8 BOM-prefixed CSV should decode correctly without leaking the BOM
    into the first header name."""
    _stage_csv(tmp_path, CSV_BOM)
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 4
    assert by_cc == {"RU": 3, "KZ": 1}


def test_read_csv_installs_missing_file_returns_none(tmp_path):
    """No file → (None, {}), не raise."""
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total is None
    assert by_cc == {}


def test_read_csv_installs_unknown_package_returns_zero(tmp_path):
    """File present, package not in any row → (0, {})."""
    _stage_csv(tmp_path, CSV_FULL)
    total, by_cc = rustore._read_csv_installs(
        tmp_path, WEEK_W20, "com.nonexistent.app",
    )
    assert total == 0
    assert by_cc == {}


def test_read_csv_installs_invalid_headers_raises(tmp_path):
    """CSV missing required app/installs columns → RuntimeError."""
    bad = tmp_path / ".metrics" / "rustore_weekly" / "2026-W20.csv"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("Foo,Bar,Baz\n1,2,3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing required headers"):
        rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)


def test_read_csv_installs_case_insensitive_package_match(tmp_path):
    """Package match is case-insensitive."""
    target = tmp_path / ".metrics" / "rustore_weekly" / "2026-W20.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "App,Installs,Country\n"
        "Website.Centry.App,5,RU\n",   # mixed case
        encoding="utf-8",
    )
    total, by_cc = rustore._read_csv_installs(tmp_path, WEEK_W20, PACKAGE_CENTRY)
    assert total == 5
    assert by_cc == {"RU": 5}


# ===================================================================
# _top_country
# ===================================================================

def test_top_country_picks_highest_share():
    top, share = rustore._top_country({"RU": 3, "KZ": 1})
    assert top == "RU"
    assert share == pytest.approx(0.75)


def test_top_country_empty_returns_none_pair():
    assert rustore._top_country({}) == (None, None)


# ===================================================================
# fetch_weekly — integration
# ===================================================================

def test_fetch_weekly_unconfigured_returns_mock(monkeypatch, rsa_keypair):
    """Without envs → mock snapshot, no HTTP / CSV calls."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="none")
    snap = rustore.fetch_weekly("centry", WEEK_W20)
    assert snap.product == "centry"
    assert snap.store == "rustore"
    assert snap.installs == 4    # _MOCK_INSTALLS[centry]
    assert snap.rating == 4.8
    assert snap.top_country == "RU"


def test_fetch_weekly_csv_missing_soft_fallback(monkeypatch, rsa_keypair, tmp_path):
    """Configured but CSV not present → installs=None, error message set,
    rating still fetched через API."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")

    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_CENTRY)
    with patch.object(
        rustore, "_repo_root", return_value=tmp_path,
    ), patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.error is not None
    assert "RuStore CSV не положен" in snap.error
    # Rating still wired through API (independent of CSV).
    assert snap.rating == pytest.approx(4.5)


def test_fetch_weekly_csv_ok_ratings_ok_returns_full_snapshot(
    monkeypatch, rsa_keypair, tmp_path,
):
    """Both paths succeed → installs + rating + top_country populated, no error."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    _stage_csv(tmp_path, CSV_FULL)

    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_CENTRY)
    with patch.object(
        rustore, "_repo_root", return_value=tmp_path,
    ), patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 4
    assert snap.rating == pytest.approx(4.5)
    assert snap.top_country == "RU"
    assert snap.top_country_share == pytest.approx(0.75)
    assert snap.error is None


def test_fetch_weekly_ratings_api_fails_csv_still_works(
    monkeypatch, rsa_keypair, tmp_path,
):
    """Auth fails → installs read OK from CSV, rating=None, error mentions API."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    _stage_csv(tmp_path, CSV_FULL)

    # Auth fails with HTTP 401
    auth_resp = _mock_response({"err": "bad signature"}, status=401)
    with patch.object(
        rustore, "_repo_root", return_value=tmp_path,
    ), patch.object(
        rustore._http, "fetch_with_retry", return_value=auth_resp,
    ):
        snap = rustore.fetch_weekly("centry", WEEK_W20)

    assert snap.installs == 4   # CSV read OK
    assert snap.rating is None
    assert snap.error is not None
    assert "RuStore reviews API" in snap.error


def test_fetch_weekly_for_diktum_isolates_correctly(
    monkeypatch, rsa_keypair, tmp_path,
):
    """Same CSV, requesting Diktum → only Diktum rows summed."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    _stage_csv(tmp_path, CSV_FULL)

    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_EMPTY)
    with patch.object(
        rustore, "_repo_root", return_value=tmp_path,
    ), patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_weekly("diktum", WEEK_W20)

    assert snap.installs == 2   # Diktum got 2 installs in fixture (RU)
    assert snap.rating is None  # empty reviews fixture
    assert snap.top_country == "RU"
    assert snap.error is None


# ===================================================================
# fetch_previous
# ===================================================================

def test_fetch_previous_unconfigured_returns_mock(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="none")
    snap = rustore.fetch_previous("centry", WEEK_W20)
    assert snap.installs == 3      # _MOCK_PREV[centry]
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_previous_shifts_week_by_7_days(monkeypatch, rsa_keypair, tmp_path):
    """Configured → calls fetch_weekly with week_start - 7 days.

    Previous week CSV (W19) absent in fixture → installs=None соft-fallback.
    """
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    # Don't stage W19 → CSV missing → soft-fallback.
    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_EMPTY)
    with patch.object(
        rustore, "_repo_root", return_value=tmp_path,
    ), patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_previous("centry", WEEK_W20)

    assert snap.week_start == dt.date(2026, 5, 4)
    assert snap.installs is None
    assert snap.error is not None
    assert "RuStore CSV не положен" in snap.error
