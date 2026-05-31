"""Worked examples for the ``mock_http`` fixture.

Two patterns:

1. **Pure requests** — register a canned response, fire a request, assert on
   the outgoing call. Useful for proving the fixture works.

2. **Boox.api_call** — construct a client with ``skip_init=True`` (so the
   constructor doesn't try to talk to the real API) and exercise the
   ``api_call`` helper. This is the pattern the per-endpoint test suites in
   #4–#8 will use.

Added by #2 (Mock HTTP layer for unit tests).
"""

import boox


def test_mock_http_pure_requests(mock_http):
    """Pattern 1: pure-requests use of the fixture."""
    import requests

    mock_http.get(
        "https://example.com/api",
        json={"hello": "world"},
        status=200,
        headers={"x-custom": "yes"},
    )
    r = requests.get(
        "https://example.com/api",
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 200
    assert r.json() == {"hello": "world"}
    assert r.headers["x-custom"] == "yes"
    # Outgoing request capture: assert auth header was sent.
    assert len(mock_http.calls) == 1
    assert mock_http.calls[0].request.headers["Authorization"] == "Bearer test-token"


def test_mock_http_boox_api_call(mock_http):
    """Pattern 2: exercise Boox.api_call without the full __init__ chain.

    ``skip_init=True`` bypasses the constructor's network calls
    (``users/me``, ``users/getDevice``, etc.) so we can unit-test the helper
    in isolation.
    """
    mock_http.get(
        "https://push.boox.com/api/1/users/me",
        json={"result_code": 0, "data": {"id": 387791, "uid": "abc123"}},
        status=200,
    )
    config = {"default": {"cloud": "push.boox.com", "token": "test-token"}}
    client = boox.Boox(config, skip_init=True)
    client.token = "test-token"
    result = client.api_call("users/me")
    assert result["result_code"] == 0
    assert result["data"]["id"] == 387791
    # Outgoing request: assert Bearer header set + URL shape.
    assert len(mock_http.calls) == 1
    req = mock_http.calls[0].request
    assert req.url == "https://push.boox.com/api/1/users/me"
    assert req.headers["Authorization"] == "Bearer test-token"


def test_mock_http_captures_post_body(mock_http):
    """Pattern 3: assert on outgoing POST body."""
    mock_http.post(
        "https://push.boox.com/api/1/users/me",
        json={"result_code": 0, "data": "ok"},
        status=200,
    )
    config = {"default": {"cloud": "push.boox.com", "token": "test-token"}}
    client = boox.Boox(config, skip_init=True)
    client.token = "test-token"
    # api_call switches to POST when a non-empty data dict is provided.
    client.api_call("users/me", data={"k": "v"})
    req = mock_http.calls[0].request
    assert req.method == "POST"
    # Body is JSON-encoded per Boox's api_call helper.
    import json as _json
    assert _json.loads(req.body) == {"k": "v"}
