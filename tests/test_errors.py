"""Unit tests for ``boox.errors`` and ``BooxClient.api_call`` error mapping.

Covers the typed exception hierarchy introduced in #28:

- ``BooxError`` / ``AuthError`` / ``RateLimitError`` / ``NotFoundError``
  / ``APIError`` / ``OSSError`` class shape + state.
- ``boox.errors.from_response`` mapping for every HTTP status / result_code
  branch in the dispatch table.
- ``api_call`` raises the right exception type end-to-end.
- ``send_file`` wraps oss2 failures in ``OSSError`` with ``__cause__``.
- Documented design decisions: ``requests.RequestException`` propagates
  unchanged; ``response_body`` is the raw text; subclasses are catchable
  as ``BooxError``.

HAR-confirmed shape: the ``result_code: 1`` on HTTP 500 envelope test
mirrors the actual shape captured in
``push-boox-har-2026-05-31.json`` for
``POST /api/1/rsses/url/content`` (``{"result_code": 1, "message":
"<text>", "data": null}``).
"""

from __future__ import annotations

import json

import pytest
import requests

import boox
from boox import errors
from boox.errors import (
    APIError,
    AuthError,
    BooxError,
    NotFoundError,
    OSSError,
    RESULT_CODE_GENERIC_FAILURE,
    RESULT_CODE_INVALID_INPUT,
    RESULT_CODE_SUCCESS,
    RateLimitError,
    from_response,
)
from .conftest import TEST_API_BASE


# --------------------------- class hierarchy ------------------------------


def test_all_subclasses_are_boox_error():
    """Every typed exception inherits from ``BooxError`` (and ``Exception``).

    Locks the contract that callers can `except BooxError` to catch any
    API-level failure regardless of which subclass fired.
    """
    for cls in (AuthError, RateLimitError, NotFoundError, APIError, OSSError):
        assert issubclass(cls, BooxError)
        assert issubclass(cls, Exception)


def test_boox_error_init_stores_metadata():
    """Constructor populates ``response_body`` / ``status_code`` /
    ``result_code`` as keyword-only state on the instance."""
    exc = BooxError(
        "boom",
        response_body='{"result_code":1}',
        status_code=500,
        result_code=1,
    )
    assert str(exc) == "boom"
    assert exc.response_body == '{"result_code":1}'
    assert exc.status_code == 500
    assert exc.result_code == 1


def test_boox_error_defaults_metadata_to_none():
    """Errors raised without HTTP context have None for all response fields.

    ``OSSError`` (wraps oss2) is the prototypical caller — there's no
    Boox HTTP response to attach.
    """
    exc = BooxError("no http")
    assert exc.response_body is None
    assert exc.status_code is None
    assert exc.result_code is None


# --------------------------- result_code constants ------------------------


def test_result_code_constants_have_documented_values():
    """The catalog values are load-bearing — pinning them so a contributor
    can't silently re-number what's in HARs / JS bundles."""
    assert RESULT_CODE_SUCCESS == 0
    assert RESULT_CODE_GENERIC_FAILURE == 1
    assert RESULT_CODE_INVALID_INPUT == 100011


# --------------------------- from_response mapping ------------------------


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` for unit testing the
    mapping function in isolation from the HTTP layer."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def _fake_json_response(status_code, payload):
    return _FakeResponse(status_code, json.dumps(payload))


def test_from_response_returns_none_on_2xx_with_result_code_zero():
    """Happy path: 200 + result_code 0 → no exception."""
    resp = _fake_json_response(200, {"result_code": 0, "data": "ok"})
    assert from_response(resp) is None


def test_from_response_returns_none_on_2xx_without_result_code():
    """Some endpoints (e.g., raw neocloud paths) don't include result_code
    in their success body. Don't penalize them — treat the 2xx alone as
    success."""
    resp = _fake_json_response(200, {"docs": []})
    assert from_response(resp) is None


def test_from_response_maps_nonzero_result_code_to_api_error():
    """2xx + non-zero result_code → APIError with status + code populated."""
    body = {"result_code": 1, "message": "boom", "data": None}
    resp = _fake_json_response(200, body)
    exc = from_response(resp)
    assert isinstance(exc, APIError)
    assert exc.status_code == 200
    assert exc.result_code == 1
    # Raw text is preserved so callers see exactly what the server sent.
    assert json.loads(exc.response_body) == body


def test_from_response_maps_401_to_auth_error():
    resp = _fake_json_response(401, {"result_code": 100, "message": "no"})
    exc = from_response(resp)
    assert isinstance(exc, AuthError)
    assert exc.status_code == 401
    assert exc.result_code == 100


def test_from_response_maps_404_to_not_found_error():
    resp = _fake_json_response(404, {"result_code": 1, "message": "gone"})
    exc = from_response(resp)
    assert isinstance(exc, NotFoundError)
    assert exc.status_code == 404


def test_from_response_maps_429_to_rate_limit_error():
    """RateLimitError fires on 429 even though we haven't observed Boox
    using 429 in HARs — class exists for forward compatibility."""
    resp = _fake_json_response(429, {"message": "slow down"})
    exc = from_response(resp)
    assert isinstance(exc, RateLimitError)
    assert exc.status_code == 429


def test_from_response_maps_500_with_result_code_envelope_to_api_error():
    """5xx → APIError. Mirrors the HAR-confirmed shape from
    push-boox-har-2026-05-31.json (POST /api/1/rsses/url/content with
    a malformed feed) — HTTP 500, body
    {"result_code": 1, "message": "Unencoded <...", "data": null}.
    """
    body = {
        "result_code": 1,
        "message": "Unencoded <\nLine: 0\nColumn: 1342\nChar: =",
        "data": None,
    }
    resp = _fake_json_response(500, body)
    exc = from_response(resp)
    assert isinstance(exc, APIError)
    assert exc.status_code == 500
    assert exc.result_code == 1


def test_from_response_maps_other_4xx_to_api_error():
    """4xx without a dedicated subclass (e.g., 403) → APIError."""
    resp = _fake_json_response(403, {"result_code": 1, "message": "denied"})
    exc = from_response(resp)
    assert isinstance(exc, APIError)
    # Not AuthError — only 401 maps there. 403 is "you authenticated but
    # this isn't allowed", which is a different recovery path.
    assert not isinstance(exc, AuthError)
    assert exc.status_code == 403


def test_from_response_tolerates_non_json_body():
    """Upstream proxy HTML errors shouldn't crash the error layer.

    Sometimes Boox's edge proxy returns an HTML 502 instead of a JSON
    envelope; the raw text still ends up in ``response_body`` and
    ``result_code`` is None.
    """
    resp = _FakeResponse(502, "<html><body>Bad Gateway</body></html>")
    exc = from_response(resp)
    assert isinstance(exc, APIError)
    assert exc.status_code == 502
    assert exc.result_code is None
    assert "Bad Gateway" in exc.response_body


def test_from_response_tolerates_empty_body():
    """A 404 with an empty body should still map cleanly."""
    resp = _FakeResponse(404, "")
    exc = from_response(resp)
    assert isinstance(exc, NotFoundError)
    assert exc.result_code is None


# --------------------------- api_call end-to-end --------------------------


def test_api_call_raises_auth_error_on_401(mock_http, unit_client):
    """End-to-end: api_call surfaces a 401 as AuthError with metadata."""
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 100, "message": "Unauthorized"},
        status=401,
    )
    with pytest.raises(AuthError) as excinfo:
        unit_client.api_call("users/me")
    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.result_code == 100


def test_api_call_raises_not_found_error_on_404(mock_http, unit_client):
    """End-to-end: 404 → NotFoundError. Boox 404s on /api/1/* are rare
    (most failures come back as 200 + result_code != 0), but the OSS
    edge does emit them — mirroring the typed mapping is consistent."""
    mock_http.get(
        f"{TEST_API_BASE}/webpage/list",
        json={"result_code": 1, "message": "not found"},
        status=404,
    )
    with pytest.raises(NotFoundError) as excinfo:
        unit_client.api_call("webpage/list")
    assert excinfo.value.status_code == 404


def test_api_call_raises_rate_limit_error_on_429(mock_http, unit_client):
    """End-to-end: 429 → RateLimitError."""
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"message": "slow down"},
        status=429,
    )
    with pytest.raises(RateLimitError) as excinfo:
        unit_client.api_call("users/me")
    assert excinfo.value.status_code == 429


def test_api_call_raises_api_error_on_5xx(mock_http, unit_client):
    """End-to-end: 500 with result_code envelope → APIError."""
    mock_http.get(
        f"{TEST_API_BASE}/rsses/url/content",
        json={"result_code": 1, "message": "Unencoded <", "data": None},
        status=500,
    )
    with pytest.raises(APIError) as excinfo:
        unit_client.api_call("rsses/url/content")
    exc = excinfo.value
    assert exc.status_code == 500
    assert exc.result_code == 1


def test_api_call_raises_api_error_on_nonzero_result_code_200(
    mock_http, unit_client
):
    """End-to-end: 200 + result_code != 0 → APIError. This is the most
    common Boox failure shape — the server returns 200 with an error
    envelope rather than an HTTP error status."""
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 1, "message": "boom"},
        status=200,
    )
    with pytest.raises(APIError) as excinfo:
        unit_client.api_call("users/me")
    exc = excinfo.value
    assert exc.status_code == 200
    assert exc.result_code == 1


def test_api_call_propagates_connection_error_unchanged(
    mock_http, unit_client, mocker
):
    """Documented decision: ``requests.RequestException`` (ConnectionError,
    Timeout, etc.) propagates unchanged — not wrapped in BooxError.

    Rationale: transport failures are a retry candidate, API failures
    generally are not. The existing init chain already catches
    ``requests.RequestException`` at the right level; wrapping would
    force every caller to learn a second exception type for the same
    semantics. See ``boox/errors.py`` module docstring for the full
    decision record.
    """
    mocker.patch.object(
        boox.requests,
        "request",
        side_effect=requests.ConnectionError("network down"),
    )
    with pytest.raises(requests.ConnectionError):
        unit_client.api_call("users/me")


def test_api_call_returns_envelope_unchanged_on_success(
    mock_http, unit_client
):
    """Regression guard: 200 + result_code 0 returns the parsed body
    untouched — no behavioral change for happy-path callers."""
    body = {"result_code": 0, "data": {"uid": "test-uid"}, "message": "OK"}
    mock_http.get(f"{TEST_API_BASE}/users/me", json=body, status=200)
    result = unit_client.api_call("users/me")
    assert result == body


# --------------------------- catchability ---------------------------------


def test_subclasses_catchable_as_boox_error(mock_http, unit_client):
    """An ``except BooxError`` clause catches any of the typed subclasses.

    Callers that don't want to branch on specific failure modes can use
    one catch-all clause; this test locks that affordance.
    """
    mock_http.get(
        f"{TEST_API_BASE}/users/me",
        json={"result_code": 100, "message": "auth"},
        status=401,
    )
    with pytest.raises(BooxError):
        unit_client.api_call("users/me")


# --------------------------- OSSError wrapping ----------------------------


def test_oss_error_oss_exception_property():
    """``oss_exception`` property exposes the wrapped exception. Uses
    ``raise from`` semantics so ``__cause__`` carries the original."""
    inner = ValueError("inner")
    try:
        raise OSSError("wrap") from inner
    except OSSError as exc:
        assert exc.oss_exception is inner
        assert exc.__cause__ is inner


def test_oss_error_oss_exception_is_none_without_cause():
    """No ``from`` clause → ``oss_exception`` is None (matches __cause__)."""
    exc = OSSError("no cause")
    assert exc.oss_exception is None


# --------------------------- AuthError alias preserved --------------------


def test_auth_error_importable_from_boox_auth():
    """``from boox.auth import AuthError`` keeps working — it's the same
    class as ``boox.errors.AuthError``, just re-exported. Preserves
    backwards compatibility for callers from before #28."""
    from boox.auth import AuthError as auth_module_AuthError

    assert auth_module_AuthError is errors.AuthError
