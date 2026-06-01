"""Unit test suite — authenticated session endpoints.

In scope: token-bearing calls only (``users/me``, ``users/getDevice``,
``im/getSig``, ``config/buckets``, ``config/stss``), plus the
``Boox.__init__`` chain that exercises four of them in sequence.

Out of scope: the token-acquisition flow (sendVerifyCode /
signupByPhoneOrEmail / Aliyun captcha) — that lives behind a captcha and
will be addressed in Phase 5. Phase 0 assumes the JWT is already valid.

Added by #4 (Unit test suite — authenticated session).
"""

import json

import pytest

import boox
from .conftest import TEST_API_BASE, TEST_CLOUD, TEST_TOKEN


# Sample responses cribbed from real HAR captures (with identifying data
# scrubbed). Keeping them realistic helps tests catch shape drift.
_USERS_ME_DATA = {
    "id": 387791,
    "uid": "user-uid-fixture",
    "phone": "5550000000",
    "area_code": "+1",
    "login_type": "phone",
}
_GET_DEVICE_DATA = {"deviceList": []}
_IM_GET_SIG_DATA = {"sig": "sig-string"}
_CONFIG_BUCKETS_DATA = {
    "onyx-cloud": {
        "bucket": "onyx-cloud-test",
        "aliEndpoint": "oss-test.aliyuncs.com",
    }
}
_CONFIG_STSS_DATA = {
    "AccessKeyId": "STS.FixtureKey",  # pragma: allowlist secret
    "AccessKeySecret": "FixtureSecret",  # pragma: allowlist secret
    "SecurityToken": "FixtureToken",  # pragma: allowlist secret
}


# --------------------------- Per-endpoint shape tests ---------------------


@pytest.mark.parametrize(
    "endpoint,params,data_payload",
    [
        ("users/me", None, _USERS_ME_DATA),
        ("users/getDevice", None, _GET_DEVICE_DATA),
        ("im/getSig", {"user": "user-uid-fixture"}, _IM_GET_SIG_DATA),
        ("config/buckets", None, _CONFIG_BUCKETS_DATA),
        ("config/stss", None, _CONFIG_STSS_DATA),
    ],
)
def test_authenticated_get_request_shape(
    mock_http, unit_client, endpoint, params, data_payload
):
    """Each authenticated GET sends Bearer auth and parses the JSON envelope."""
    mock_http.get(
        f"{TEST_API_BASE}/{endpoint}",
        json={"result_code": 0, "data": data_payload},
        status=200,
    )
    kwargs = {"params": params} if params else {}
    resp = unit_client.api_call(endpoint, **kwargs)
    assert resp["result_code"] == 0
    assert resp["data"] == data_payload

    req = mock_http.calls[0].request
    assert req.method == "GET"
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"


def test_api_call_uses_bearer_header(mock_http, unit_client):
    """Sanity check: the Authorization header is exactly the Bearer form."""
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 0, "data": _USERS_ME_DATA},
    )
    unit_client.api_call("users/me")
    auth = mock_http.calls[0].request.headers["Authorization"]
    assert auth.startswith("Bearer ")
    assert auth == f"Bearer {TEST_TOKEN}"


# --------------------------- Init chain ------------------------------------


def test_init_chain_invokes_expected_endpoints(mock_http, boox_config):
    """``Boox(config)`` (without skip_init) calls the four init endpoints in
    order and stores derived attributes from their responses."""
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 0, "data": _USERS_ME_DATA},
    )
    mock_http.get(
        f"{TEST_API_BASE}/users/getDevice",
        json={"result_code": 0, "data": _GET_DEVICE_DATA},
    )
    mock_http.get(
        f"{TEST_API_BASE}/im/getSig",
        json={"result_code": 0, "data": _IM_GET_SIG_DATA},
    )
    mock_http.get(
        f"{TEST_API_BASE}/config/buckets",
        json={"result_code": 0, "data": _CONFIG_BUCKETS_DATA},
    )

    client = boox.Boox(boox_config)

    assert client.userid == "user-uid-fixture"
    assert client.bucket_name == "onyx-cloud-test"
    assert client.endpoint == "oss-test.aliyuncs.com"
    assert client.cloud == TEST_CLOUD

    # Confirm order — Boox.__init__ does users/me → getDevice → im/getSig →
    # config/buckets. Asserting paths in that order locks the chain.
    paths = [c.request.url.split("/api/1/", 1)[1].split("?")[0] for c in mock_http.calls]
    assert paths[:4] == [
        "users/me",
        "users/getDevice",
        "im/getSig",
        "config/buckets",
    ]


def test_init_chain_passes_uid_to_im_get_sig(mock_http, boox_config):
    """``im/getSig`` is called with ``user`` query param set to the uid
    returned from ``users/me``."""
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 0, "data": _USERS_ME_DATA},
    )
    mock_http.get(
        f"{TEST_API_BASE}/users/getDevice",
        json={"result_code": 0, "data": _GET_DEVICE_DATA},
    )
    mock_http.get(
        f"{TEST_API_BASE}/im/getSig",
        json={"result_code": 0, "data": _IM_GET_SIG_DATA},
    )
    mock_http.get(
        f"{TEST_API_BASE}/config/buckets",
        json={"result_code": 0, "data": _CONFIG_BUCKETS_DATA},
    )

    boox.Boox(boox_config)

    im_call = [c for c in mock_http.calls if "im/getSig" in c.request.url][0]
    # responses preserves the query string in the URL.
    assert "user=user-uid-fixture" in im_call.request.url


# --------------------------- Error paths -----------------------------------


def test_authenticated_call_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` on a 2xx response raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 1, "message": "boom", "data": None},
        status=200,  # Boox often returns 200 with result_code != 0
    )
    with pytest.raises(APIError) as excinfo:
        unit_client.api_call("users/me")
    exc = excinfo.value
    assert exc.status_code == 200
    assert exc.result_code == 1
    assert "boom" in exc.response_body


def test_authenticated_call_on_401_raises_auth_error(mock_http, unit_client):
    """401 from the cloud raises ``AuthError`` (#28)."""
    from boox.errors import AuthError

    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 100, "message": "Unauthorized", "data": None},
        status=401,
    )
    with pytest.raises(AuthError) as excinfo:
        unit_client.api_call("users/me")
    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.result_code == 100


# --------------------------- POST + data path -----------------------------


def test_api_call_switches_to_post_when_data_given(mock_http, unit_client):
    """``api_call`` flips to POST + sets JSON Content-Type when ``data`` is
    a non-empty mapping — the path used by ``saveAndPush``, ``subscribe/sub``,
    etc.
    """
    mock_http.post(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 0, "data": "ok"},
    )
    unit_client.api_call("users/me", data={"key": "value"})
    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.headers.get("Content-Type", "").startswith("application/json")
    assert json.loads(req.body) == {"key": "value"}
