"""Live-API smoke test for token validity.

Hits ``users/me`` against the live cloud and asserts a sensible response.
Skipped by default — set ``BOOX_RUN_LIVE_TESTS=1`` and provide a valid token
via one of the sources documented in ``conftest.py::live_token``.

Added by #3 (Live-API integration test gating). The full end-to-end live
smoke suite (push → list → delete round-trip) lands in #9.
"""

import pytest

import boox


@pytest.mark.live
def test_live_users_me(live_token):
    """Token-bearing ``users/me`` call returns a non-empty uid."""
    config = {"default": {"cloud": "push.boox.com", "token": live_token}}
    client = boox.Boox(config, skip_init=True)
    client.token = live_token
    resp = client.api_call("users/me")
    assert resp["result_code"] == 0, f"users/me returned: {resp}"
    data = resp["data"]
    assert data.get("uid"), "users/me response has no uid"
    assert data.get("id"), "users/me response has no numeric id"
