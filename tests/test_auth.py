"""Unit tests for ``boox.auth``.

Covers the four module-level JWT helpers (``load_token``, ``decode_jwt``,
``is_expired``, ``time_to_expiry``) plus the ``Auth.mint_sync_session``
method that runtime-derives the SyncGatewaySession cookie from the
Bearer JWT.

Test data: real JWTs are minted at test time with deterministic payloads
so we can assert on decoded claims. We do NOT sign them — Boox's tokens
are HS256 with a secret we don't have, but our ``decode_jwt`` doesn't
verify signatures (intentional — see module docstring), so an arbitrary
signature segment is fine.

Added by #27 (Phase 1 auth module — token loading + JWT validation).
"""

import base64
import json
import time

import pytest
import requests

import boox
from boox import auth
from .conftest import TEST_API_BASE, TEST_TOKEN, TEST_SYNC_TOKEN


# --------------------------- JWT fixtures ---------------------------------


def _b64url_encode(data: bytes) -> str:
    """Encode bytes as base64url without padding (matches JWT segments)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_jwt(payload: dict, header: dict | None = None, sig: str = "sig") -> str:
    """Build a syntactically valid JWT with the given payload.

    Signature segment is a fixed placeholder — our decoder never verifies,
    so the bytes don't matter as long as they form a valid third segment.
    """
    header = header or {"alg": "HS256", "typ": "JWT"}
    header_seg = _b64url_encode(json.dumps(header).encode())
    payload_seg = _b64url_encode(json.dumps(payload).encode())
    return f"{header_seg}.{payload_seg}.{sig}"


@pytest.fixture
def valid_jwt():
    """JWT shaped like Boox's real tokens, expiring in 30 days."""
    now = int(time.time())
    payload = {
        "id": 387791,
        "loginType": "phone",
        "iat": now,
        "exp": now + 30 * 24 * 3600,
    }
    return _make_jwt(payload)


@pytest.fixture
def expired_jwt():
    """JWT whose ``exp`` is in the past."""
    now = int(time.time())
    payload = {
        "id": 387791,
        "loginType": "phone",
        "iat": now - 8 * 24 * 3600,
        "exp": now - 24 * 3600,
    }
    return _make_jwt(payload)


# --------------------------- decode_jwt -----------------------------------


def test_decode_jwt_returns_payload_dict(valid_jwt):
    """A well-formed JWT decodes to its payload claims."""
    payload = auth.decode_jwt(valid_jwt)
    assert payload["id"] == 387791
    assert payload["loginType"] == "phone"
    assert isinstance(payload["iat"], int)
    assert isinstance(payload["exp"], int)


def test_decode_jwt_real_har_shape():
    """Decoding the actual JWT captured in the 2026-05-31 settings HAR
    returns the documented payload — locks the HAR-confirmed shape."""
    har_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJpZCI6Mzg3NzkxLCJsb2dpblR5cGUiOiJwaG9uZSIsImlhdCI6MTc3MTgxMDA3NCwiZXhwIjoxNzg3MzYyMDc0fQ"
        ".8ZytG4lKV5KiL1XrrZTnl6sQdEAJkZVW8fZ7qacln_0"
    )
    payload = auth.decode_jwt(har_token)
    assert payload == {
        "id": 387791,
        "loginType": "phone",
        "iat": 1771810074,
        "exp": 1787362074,
    }


def test_decode_jwt_raises_on_empty():
    with pytest.raises(auth.AuthError, match="empty"):
        auth.decode_jwt("")


def test_decode_jwt_raises_on_non_string():
    with pytest.raises(auth.AuthError):
        auth.decode_jwt(None)  # type: ignore[arg-type]


def test_decode_jwt_raises_on_wrong_segment_count():
    """Two-segment or four-segment strings aren't JWTs."""
    with pytest.raises(auth.AuthError, match="3 segments"):
        auth.decode_jwt("a.b")
    with pytest.raises(auth.AuthError, match="3 segments"):
        auth.decode_jwt("a.b.c.d")


def test_decode_jwt_raises_on_non_json_payload():
    """Base64url-valid but non-JSON payload raises with a clear message."""
    bad_payload_seg = _b64url_encode(b"not-json-just-bytes")
    token = f"header.{bad_payload_seg}.sig"
    with pytest.raises(auth.AuthError, match="json"):
        auth.decode_jwt(token)


# --------------------------- time_to_expiry / is_expired ------------------


def test_time_to_expiry_positive_for_fresh_token(valid_jwt):
    seconds = auth.time_to_expiry(valid_jwt)
    # Within a generous window — fixture said 30 days.
    assert 29 * 24 * 3600 < seconds <= 30 * 24 * 3600


def test_time_to_expiry_negative_for_expired_token(expired_jwt):
    assert auth.time_to_expiry(expired_jwt) < 0


def test_time_to_expiry_raises_when_exp_missing():
    """A JWT without ``exp`` is malformed for our purposes."""
    token = _make_jwt({"id": 1, "loginType": "phone", "iat": int(time.time())})
    with pytest.raises(auth.AuthError, match="exp"):
        auth.time_to_expiry(token)


def test_is_expired_false_for_fresh_token(valid_jwt):
    assert auth.is_expired(valid_jwt) is False


def test_is_expired_true_for_expired_token(expired_jwt):
    assert auth.is_expired(expired_jwt) is True


def test_is_expired_respects_leeway():
    """A token expiring in 30 seconds is "expired" with the default 60s leeway
    but not with 0s leeway — guards the borderline-refresh case."""
    now = int(time.time())
    token = _make_jwt({"id": 1, "loginType": "phone", "iat": now, "exp": now + 30})
    assert auth.is_expired(token) is True  # default leeway 60
    assert auth.is_expired(token, leeway_seconds=0) is False


# --------------------------- load_token -----------------------------------


def test_load_token_returns_none_when_no_source(monkeypatch, tmp_path):
    """No env, no config, no file — None."""
    monkeypatch.delenv("BOOX_TOKEN", raising=False)
    monkeypatch.delenv("BOOX_SECRETS_FILE", raising=False)
    assert auth.load_token() is None


def test_load_token_from_env(monkeypatch):
    monkeypatch.setenv("BOOX_TOKEN", "env-token-value")  # pragma: allowlist secret
    monkeypatch.delenv("BOOX_SECRETS_FILE", raising=False)
    assert auth.load_token() == "env-token-value"


def test_load_token_from_config(monkeypatch):
    """Config mapping is consulted when env var is unset."""
    monkeypatch.delenv("BOOX_TOKEN", raising=False)
    monkeypatch.delenv("BOOX_SECRETS_FILE", raising=False)
    config = {"default": {"token": "config-token-value", "cloud": "x"}}
    assert auth.load_token(config=config) == "config-token-value"


def test_load_token_env_wins_over_config(monkeypatch):
    """Documented priority: env var beats config."""
    monkeypatch.setenv("BOOX_TOKEN", "env-wins")  # pragma: allowlist secret
    config = {"default": {"token": "config-loses"}}
    assert auth.load_token(config=config) == "env-wins"


def test_load_token_from_env_file(monkeypatch, tmp_path):
    """Explicit env_file path read via KEY=VALUE parsing."""
    monkeypatch.delenv("BOOX_TOKEN", raising=False)
    monkeypatch.delenv("BOOX_SECRETS_FILE", raising=False)
    env_path = tmp_path / "boox.env"
    env_path.write_text(
        '# header comment\nBOOX_TOKEN="file-token-value"\nOTHER=ignored\n'
    )
    assert auth.load_token(env_file=env_path) == "file-token-value"


def test_load_token_from_boox_secrets_file_env(monkeypatch, tmp_path):
    """BOOX_SECRETS_FILE env var resolves to a file we parse for the var."""
    monkeypatch.delenv("BOOX_TOKEN", raising=False)
    env_path = tmp_path / "secrets.env"
    env_path.write_text("export BOOX_TOKEN='secrets-file-token'\n")
    monkeypatch.setenv("BOOX_SECRETS_FILE", str(env_path))
    assert auth.load_token() == "secrets-file-token"


def test_load_token_env_file_missing_file_returns_none(monkeypatch, tmp_path):
    """Pointing at a nonexistent file isn't an error — just falls through."""
    monkeypatch.delenv("BOOX_TOKEN", raising=False)
    monkeypatch.delenv("BOOX_SECRETS_FILE", raising=False)
    missing = tmp_path / "does-not-exist.env"
    assert auth.load_token(env_file=missing) is None


# --------------------------- Auth.mint_sync_session -----------------------


# Documented users/syncToken response shape (HAR-confirmed,
# settings-har-2026-05-31.har):
#   {"result_code":0,
#    "data":{"session_id":"<hex>","expires":"<ISO8601>",
#            "cookie_name":"SyncGatewaySession","channels":[]},
#    "message":"SUCCESS",
#    "tokenExpiredAt":<epoch>}
_MINTED_SESSION_DATA = {
    "session_id": "4bafb29c120c132ad094459eae68bdd2d57c17b7",  # pragma: allowlist secret
    "expires": "2026-06-22T13:55:18Z",
    "cookie_name": "SyncGatewaySession",
    "channels": [],
}


def test_mint_sync_session_happy_path(mock_http, unit_client):
    """Mocked /api/1/users/syncToken populates ``client.sync_token``."""
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={
            "result_code": 0,
            "data": _MINTED_SESSION_DATA,
            "message": "SUCCESS",
            "tokenExpiredAt": 1787362074,
        },
    )
    data = unit_client.auth.mint_sync_session()

    assert data == _MINTED_SESSION_DATA
    assert unit_client.sync_token == _MINTED_SESSION_DATA["session_id"]


def test_mint_sync_session_sends_bearer_auth(mock_http, unit_client):
    """The mint call uses the Bearer JWT auth — Sync Gateway cookie auth
    is for ``/neocloud/*``, not ``/api/1/users/syncToken``."""
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={"result_code": 0, "data": _MINTED_SESSION_DATA, "message": "OK"},
    )
    unit_client.auth.mint_sync_session()
    auth_header = mock_http.calls[0].request.headers["Authorization"]
    assert auth_header == f"Bearer {TEST_TOKEN}"


def test_mint_sync_session_raises_on_nonzero_result_code(mock_http, unit_client):
    """A non-zero ``result_code`` surfaces as ``AuthError`` — caller can
    fall back to the cached cookie."""
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={"result_code": 4001, "message": "session denied", "data": None},
    )
    with pytest.raises(auth.AuthError, match="result_code"):
        unit_client.auth.mint_sync_session()


def test_mint_sync_session_raises_when_session_id_missing(mock_http, unit_client):
    """Result_code 0 but no session_id is still a failure — protects
    against unexpected response-shape drift."""
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={"result_code": 0, "data": {}, "message": "OK"},
    )
    with pytest.raises(auth.AuthError, match="session_id"):
        unit_client.auth.mint_sync_session()


# --------------------------- Init-chain integration -----------------------


_INIT_USERS_ME = {
    "id": 387791,
    "uid": "user-uid-fixture",
    "phone": "5550000000",
    "area_code": "+1",
    "login_type": "phone",
}
_INIT_GET_DEVICE = {"deviceList": []}
_INIT_IM_GET_SIG = {"sig": "sig-string"}
_INIT_CONFIG_BUCKETS = {
    "onyx-cloud": {
        "bucket": "onyx-cloud-test",
        "aliEndpoint": "oss-test.aliyuncs.com",
    }
}


def _register_init_chain(mock_http):
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 0, "data": _INIT_USERS_ME},
    )
    mock_http.get(
        f"{TEST_API_BASE}/users/getDevice",
        json={"result_code": 0, "data": _INIT_GET_DEVICE},
    )
    mock_http.get(
        f"{TEST_API_BASE}/im/getSig",
        json={"result_code": 0, "data": _INIT_IM_GET_SIG},
    )
    mock_http.get(
        f"{TEST_API_BASE}/config/buckets",
        json={"result_code": 0, "data": _INIT_CONFIG_BUCKETS},
    )


def test_init_chain_mints_sync_session(mock_http, boox_config):
    """Full init runs ``users/syncToken`` after the existing 4-call chain
    and stores the minted session_id on ``client.sync_token``."""
    _register_init_chain(mock_http)
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={"result_code": 0, "data": _MINTED_SESSION_DATA, "message": "OK"},
    )

    client = boox.Boox(boox_config)

    assert client.sync_token == _MINTED_SESSION_DATA["session_id"]


def test_init_chain_falls_back_to_cached_sync_token_on_mint_failure(
    mock_http, boox_config, caplog
):
    """If users/syncToken returns an error, init falls back to the
    BOOX_SYNC_TOKEN configured in the env file / config.ini and logs
    a warning so the divergence isn't silent."""
    _register_init_chain(mock_http)
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={"result_code": 4001, "message": "denied", "data": None},
    )

    with caplog.at_level("WARNING"):
        client = boox.Boox(boox_config)

    # Fallback to the cached value from boox_config fixture.
    assert client.sync_token == TEST_SYNC_TOKEN
    assert any("falling back" in rec.getMessage() for rec in caplog.records)


def test_init_chain_warns_when_mint_fails_with_no_cached_token(
    mock_http, caplog
):
    """If the mint fails AND no cached cookie is configured, sync_token
    is None and a warning is logged. The init chain still completes —
    /api/1/* calls don't need the cookie, only /neocloud/* does."""
    config = {
        "default": {
            "cloud": "push.boox.com",
            "token": TEST_TOKEN,
            # No sync_token — simulates a fresh setup that hasn't been
            # configured with BOOX_SYNC_TOKEN.
        }
    }
    _register_init_chain(mock_http)
    mock_http.get(
        f"{TEST_API_BASE}/users/syncToken",
        json={"result_code": 4001, "message": "denied", "data": None},
    )

    with caplog.at_level("WARNING"):
        client = boox.Boox(config)

    assert client.sync_token is None
    assert any(
        "no cached" in rec.getMessage().lower() for rec in caplog.records
    )


def test_init_chain_falls_back_to_cached_on_network_failure(
    mock_http, boox_config, mocker
):
    """A ``requests.RequestException`` raised by users/syncToken (e.g. a
    transient transport failure) is handled the same way as an auth-level
    failure — fall back to cached cookie."""
    _register_init_chain(mock_http)
    # Patch the requests layer so the syncToken URL raises rather than
    # returning a response. mock_http only catches registered URLs;
    # this gives us a real ConnectionError on the specific endpoint.
    original_request = boox.requests.request

    def _failing_request(method, url, *args, **kwargs):
        if "users/syncToken" in url:
            raise requests.ConnectionError("simulated transient failure")
        return original_request(method, url, *args, **kwargs)

    mocker.patch.object(boox.requests, "request", side_effect=_failing_request)

    client = boox.Boox(boox_config)
    assert client.sync_token == TEST_SYNC_TOKEN


def test_auth_subobject_wired_on_client(boox_config):
    """``BooxClient.__init__`` wires the Auth subobject (Pattern A)."""
    client = boox.Boox(boox_config, skip_init=True)
    assert isinstance(client.auth, auth.Auth)
    # Back-reference is the client itself.
    assert client.auth._c is client
