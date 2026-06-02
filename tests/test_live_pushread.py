"""Live round-trip test for the PushRead module — push_url → list → delete.

Drives a real ``POST /api/1/webpage/url`` against push.boox.com, confirms
the new entry appears in ``webpage/list``, then deletes it via
``webpage/bat/del`` and confirms it's gone. Cleans up on any failure path
so a broken assertion mid-flight doesn't leak entries into the live inbox.

This test only needs the Bearer JWT (``BOOX_TOKEN``) — the ``webpage/*``
surface is on /api/1 and accepts Bearer auth. No SyncGatewaySession is
required, so a separate ``live_pushread_client`` fixture is provided that
skips the ``BOOX_SYNC_TOKEN`` dependency the broader ``live_client``
fixture in ``test_live_smoke.py`` has.

Skipped by default; run with:

    BOOX_RUN_LIVE_TESTS=1 \\
    BOOX_SECRETS_FILE=/path/to/secrets/boox.env \\
    pytest -m live tests/test_live_pushread.py -v

Added by #29 follow-up (push_url HAR-confirmed, round-trip).
"""

import uuid

import pytest

import boox


@pytest.fixture
def live_pushread_client(live_token):
    """Boox client wired for live PushRead tests.

    Uses ``skip_init=True`` and sets the Bearer token directly: the full
    init chain pulls in ``users/me``, ``config/buckets``, etc. and tries
    to mint a SyncGatewaySession — none of which are needed for the
    ``/api/1/webpage/*`` round-trip. Skipping init keeps this test fast
    and lets it run with only ``BOOX_TOKEN`` available.
    """
    config = {
        "default": {
            "cloud": "push.boox.com",
            "token": live_token,
            "sync_token": None,
        }
    }
    client = boox.Boox(config, skip_init=True)
    client.token = live_token
    return client


@pytest.mark.live
def test_pushread_round_trip(live_pushread_client):
    """End-to-end: push a URL, confirm it lists, delete it, confirm it's gone.

    UUID-suffixed URL so this test never collides with another live run or
    with the human's actual inbox. ``try/finally`` cleanup runs even if any
    assertion fails mid-flight — keeps the cloud-side inbox tidy.
    """
    url = f"https://example.com/round-trip-{uuid.uuid4().hex[:12]}"
    added_id = None

    try:
        # 1) Push.
        push_resp = live_pushread_client.pushread.push_url(url)
        assert push_resp["result_code"] == 0, (
            f"push_url returned non-zero: {push_resp}"
        )
        added_id = push_resp["data"]["_id"]
        assert push_resp["data"]["url"] == url

        # 2) Confirm it appears in the listing.
        list_resp = live_pushread_client.pushread.list_webpages(limit=50)
        assert list_resp["result_code"] == 0
        # ``list_webpages`` returns the standard ``{result_code, data: {
        # count, results: [...] }}`` envelope; items live under
        # ``data.results``, NOT at a top-level ``list`` key. Each item
        # carries ``_id`` at the top level (mirrors ``cbMsg.id``).
        items = list_resp.get("data", {}).get("results", [])
        ids_present = {e["_id"] for e in items}
        assert added_id in ids_present, (
            f"Just-pushed _id={added_id!r} not in listing of "
            f"{len(items)} entries."
        )

        # 3) Delete.
        del_resp = live_pushread_client.pushread.delete_webpages([added_id])
        assert del_resp["result_code"] == 0, (
            f"delete_webpages returned non-zero: {del_resp}"
        )
        deleted_id = added_id
        # Mark as cleaned up so the finally block doesn't re-delete.
        added_id = None

        # 4) Confirm it's gone.
        list_after = live_pushread_client.pushread.list_webpages(limit=50)
        assert list_after["result_code"] == 0
        items_after = list_after.get("data", {}).get("results", [])
        ids_after = {e["_id"] for e in items_after}
        assert deleted_id not in ids_after, (
            f"_id={deleted_id!r} still listed after delete."
        )

    finally:
        # Best-effort cleanup on any failure between push and delete.
        if added_id:
            try:
                live_pushread_client.pushread.delete_webpages([added_id])
            except Exception as exc:
                pytest.fail(
                    f"Test failed AND cleanup of pushed webpage (id={added_id}) "
                    f"also failed: {exc}. Manual cleanup required."
                )
