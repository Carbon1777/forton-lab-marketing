"""Unit tests for src/store_metrics/rustore.py — JWS auth + reviews + installs stub.

After 2026-05-15 canonical pivot (full-auto mode, drop manual CSV):
    - Installs path = STUB (always None + Mail.ru limitation error string).
      RuStore Public API не отдаёт installs (Brain Q3 2026-05-14 verified).
      Ждём пока Mail.ru добавит statistics endpoint в API.
    - Reviews path keeps JWS RSA-SHA512 auth + paginated comment API.

HTTP calls mocked via unittest.mock.patch on src.store_metrics._http.fetch_with_retry.
RSA test key generated once for the module (fast — 2048-bit, ~50ms).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
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
# Installs limitation constant — sanity на shape сообщения
# ===================================================================

def test_installs_limitation_error_mentions_mail_ru():
    """Stable error string должен содержать ключевые маркеры для digest."""
    msg = rustore._INSTALLS_LIMITATION_ERROR
    assert "Mail.ru" in msg
    assert "Brain Q3" in msg or "Q3" in msg
    # Указание что это constraint, не наш wire issue.
    assert "installs" in msg.lower()


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
# fetch_weekly — integration (stub installs + JWS reviews)
# ===================================================================

def test_fetch_weekly_unconfigured_returns_mock(monkeypatch, rsa_keypair):
    """Without envs → mock snapshot, no HTTP / stub calls."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="none")
    snap = rustore.fetch_weekly("centry", WEEK_W20)
    assert snap.product == "centry"
    assert snap.store == "rustore"
    assert snap.installs == 4    # _MOCK_INSTALLS[centry]
    assert snap.rating == 4.8
    assert snap.top_country == "RU"


def test_fetch_weekly_installs_returns_none_mail_ru_limitation(
    monkeypatch, rsa_keypair,
):
    """Configured → installs=None + error mentions Mail.ru limitation.

    Это canonical state — RuStore Public API не отдаёт installs endpoint.
    Reviews независимо работают через JWS auth.
    """
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")

    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_CENTRY)
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_weekly("centry", WEEK_W20)

    # Installs всегда None — Mail.ru constraint.
    assert snap.installs is None
    assert snap.error is not None
    assert "Mail.ru" in snap.error or "Brain Q3" in snap.error
    # Rating всё ещё работает через JWS reviews API.
    assert snap.rating == pytest.approx(4.5)
    # top_country None — нет installs данных.
    assert snap.top_country is None
    assert snap.top_country_share is None


def test_fetch_weekly_reviews_api_fails_installs_still_blocker(
    monkeypatch, rsa_keypair,
):
    """Auth fails → rating=None, installs всё равно None с Mail.ru blocker.

    Error string должен включать оба: primary (Mail.ru) + secondary (API).
    """
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")

    # Auth fails with HTTP 401 — reviews API недоступен
    auth_resp = _mock_response({"err": "bad signature"}, status=401)
    with patch.object(
        rustore._http, "fetch_with_retry", return_value=auth_resp,
    ):
        snap = rustore.fetch_weekly("centry", WEEK_W20)

    assert snap.installs is None
    assert snap.rating is None
    assert snap.error is not None
    # Primary error — Mail.ru limitation для installs.
    assert "Mail.ru" in snap.error
    # Secondary — reviews API failure.
    assert "RuStore reviews API" in snap.error


def test_fetch_weekly_for_diktum_isolates_correctly(
    monkeypatch, rsa_keypair,
):
    """Same envs, requesting Diktum → snapshot built for diktum package."""
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")

    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_EMPTY)
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_weekly("diktum", WEEK_W20)

    assert snap.product == "diktum"
    assert snap.store == "rustore"
    assert snap.installs is None
    assert snap.rating is None  # empty reviews fixture
    assert snap.error is not None
    assert "Mail.ru" in snap.error


# ===================================================================
# fetch_previous
# ===================================================================

def test_fetch_previous_unconfigured_returns_mock(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="none")
    snap = rustore.fetch_previous("centry", WEEK_W20)
    assert snap.installs == 3      # _MOCK_PREV[centry]
    assert snap.week_start == dt.date(2026, 5, 4)


def test_fetch_previous_shifts_week_by_7_days(monkeypatch, rsa_keypair):
    """Configured → calls fetch_weekly with week_start - 7 days.

    Installs всегда None — Mail.ru limitation независимо от недели.
    """
    pem, _ = rsa_keypair
    _set_envs(monkeypatch, pem, mode="raw")
    auth_resp = _mock_response(AUTH_RESPONSE)
    reviews_resp = _mock_response(REVIEWS_EMPTY)
    with patch.object(
        rustore._http, "fetch_with_retry", side_effect=[auth_resp, reviews_resp],
    ):
        snap = rustore.fetch_previous("centry", WEEK_W20)

    assert snap.week_start == dt.date(2026, 5, 4)
    assert snap.installs is None
    assert snap.error is not None
    assert "Mail.ru" in snap.error


# HOTFIX regression tests (smoke run 25890122345)


def test_package_for_strips_whitespace(monkeypatch):
    """GH Secret storage may add trailing newline. _package_for must strip it
    so URL paths like /public/v1/application/<package>/comment don't break.
    """
    monkeypatch.setenv("RUSTORE_PACKAGE_CENTRY", "website.centry.app\n")
    monkeypatch.setenv("RUSTORE_PACKAGE_DIKTUM", "  ru.diktumweb.diktum  ")
    assert rustore._package_for("centry") == "website.centry.app"
    assert rustore._package_for("diktum") == "ru.diktumweb.diktum"


def test_authenticate_strips_key_id_for_signing(monkeypatch, tmp_path):
    """GH Secret RUSTORE_KEY_ID may have trailing whitespace which breaks JWS
    signature (smoke run 25890122345: HTTP 400 'Invalid request format').
    _authenticate must strip key_id before signing & sending."""
    # Generate test RSA key
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    monkeypatch.setenv("RUSTORE_PRIVATE_KEY", pem_bytes.decode())
    monkeypatch.setenv("RUSTORE_KEY_ID", "  2351028465\n")   # trailing \n
    monkeypatch.setenv("RUSTORE_COMPANY_ID", "2351526569")
    monkeypatch.setenv("RUSTORE_PACKAGE_CENTRY", "website.centry.app")
    monkeypatch.setenv("RUSTORE_PACKAGE_DIKTUM", "ru.diktumweb.diktum")

    # Clear cache to force fresh auth
    rustore._TOKEN_CACHE.clear()

    captured = {}

    def fake_fetch(url, method="POST", **kwargs):
        captured["body"] = kwargs.get("json_body")
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"code": "OK", "body": {"jwe": "bearer-xyz"}}
        return m

    with patch.object(rustore._http, "fetch_with_retry", side_effect=fake_fetch):
        token = rustore._authenticate()

    assert token == "bearer-xyz"
    # keyId in request body MUST be stripped
    assert captured["body"]["keyId"] == "2351028465"
    assert "\n" not in captured["body"]["keyId"]
    assert " " not in captured["body"]["keyId"]
