"""Live smoke test for SyncGatewaySession cookie validity.

Hits ``/neocloud/`` (the Sync Gateway root) and asserts the cookie is
accepted. Skipped by default — set ``BOOX_RUN_LIVE_TESTS=1`` and provide
``BOOX_SYNC_TOKEN`` via one of the sources documented in
``conftest.py::live_sync_token``.

Added by #3 (reopened) to validate the second auth path alongside
``test_live_auth.py::test_live_users_me`` which covers Bearer.
"""

import pytest
import requests


@pytest.mark.live
def test_live_sync_gateway_root(live_sync_token):
    """Authenticated GET on /neocloud/ returns the Sync Gateway db info."""
    r = requests.get(
        "https://push.boox.com/neocloud/",
        headers={"Cookie": f"SyncGatewaySession={live_sync_token}"},
        timeout=15,
    )
    assert r.status_code == 200, (
        f"unexpected status: {r.status_code} body={r.text[:200]}"
    )
    data = r.json()
    assert data.get("db_name") == "neocloud"
    assert data.get("state") == "Online"
