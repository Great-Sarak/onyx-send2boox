"""Live round-trip test for the Subscriptions module — folder + subscribe + list.

Drives a real ``POST /api/1/subscribe/folder`` against push.boox.com,
catalog-searches for IEEE Spectrum (HAR-confirmed catalog match),
subscribes to it under the new folder, confirms via
``GET /api/1/subscribe/list``, then cleans up the user-sub and folder via
the legacy ``Boox.delete_webpages`` endpoint (catalog-side feed docs and
folders both route through ``webpage/bat/del`` per the 2026-05-31
unsubscribe finding; this stays consistent until #31 carves out
``unsubscribe()`` as its own subscriptions method).

This test only needs the Bearer JWT (``BOOX_TOKEN``) — the
``subscribe/*`` and ``rsses/*`` surfaces are both on /api/1 and accept
Bearer auth. No SyncGatewaySession required.

Skipped by default; run with:

    BOOX_RUN_LIVE_TESTS=1 \\
    BOOX_SECRETS_FILE=/path/to/secrets/boox.env \\
    pytest -m live tests/test_live_subscriptions.py -v

Added by #30 (Subscriptions module — catalog ops & folders).
"""

import uuid

import pytest

import boox
from boox.subscriptions import FeedType


_IEEE_SPECTRUM_FEED_URL = "https://spectrum.ieee.org/feeds/feed.rss"


@pytest.fixture
def live_subscriptions_client(live_token):
    """Boox client wired for live Subscriptions tests.

    ``skip_init=True`` because the /api/1/subscribe/* and /api/1/rsses/*
    surfaces both accept Bearer auth and don't need any of the
    ``users/me`` / ``config/buckets`` / SyncGatewaySession setup the full
    init chain pulls in. Same pattern as ``test_live_pushread.py``.
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
def test_subscriptions_round_trip(live_subscriptions_client):
    """End-to-end: create folder → catalog-search → subscribe → list → clean up.

    UUID-suffixed folder title so concurrent live runs never collide.
    ``try/finally`` cleanup runs even if any assertion fails mid-flight —
    keeps the cloud-side subscription list and folders tidy.
    """
    client = live_subscriptions_client
    folder_title = f"pytest-smoke-rss-{uuid.uuid4().hex[:8]}"
    folder_id = None
    user_sub_id = None

    try:
        # 1) Create folder.
        folder_resp = client.subscriptions.create_folder(
            folder_title, FeedType.RSS
        )
        assert folder_resp["result_code"] == 0, (
            f"create_folder returned non-zero: {folder_resp}"
        )
        folder_id = folder_resp["data"]["_id"]
        assert folder_resp["data"]["title"] == folder_title
        assert folder_resp["data"]["sourceType"] == int(FeedType.RSS)

        # 2) Catalog-search for IEEE Spectrum (HAR-confirmed catalog hit).
        search_resp = client.subscriptions.search_catalog(
            _IEEE_SPECTRUM_FEED_URL, FeedType.RSS
        )
        assert search_resp["result_code"] == 0
        assert search_resp["data"]["count"] >= 1, (
            f"Expected IEEE Spectrum to be in the Boox catalog. "
            f"Got count={search_resp['data']['count']}. "
            f"Catalog may have changed; update the live-test feed URL."
        )
        feed_id = search_resp["data"]["results"][0]["_id"]

        # 3) Subscribe to it under the test folder.
        sub_resp = client.subscriptions.subscribe(
            feed_id=feed_id, parent_folder_id=folder_id
        )
        assert sub_resp["result_code"] == 0, (
            f"subscribe returned non-zero: {sub_resp}"
        )
        user_sub_id = sub_resp["data"]["_id"]
        assert sub_resp["data"]["parent"] == folder_id
        assert sub_resp["data"]["subFrom"] == feed_id

        # 4) List subscriptions, assert the new one appears.
        list_resp = client.subscriptions.list_subscriptions(FeedType.RSS)
        assert list_resp["result_code"] == 0
        # The list mixes folders and feeds; walk both top-level and
        # children for the new user-sub _id.
        all_ids = set()
        for entry in list_resp["data"]["results"]:
            all_ids.add(entry["_id"])
            for child in entry.get("children", []):
                all_ids.add(child["_id"])
        assert user_sub_id in all_ids, (
            f"Just-subscribed _id={user_sub_id!r} not in listing of "
            f"{list_resp['data']['count']} entries."
        )

    finally:
        # Best-effort cleanup: unsubscribe (delete user-sub record), then
        # delete the folder. Both route through ``webpage/bat/del`` per
        # the 2026-05-31 unified-endpoint finding.
        cleanup_ids = [
            doc_id for doc_id in (user_sub_id, folder_id) if doc_id
        ]
        if cleanup_ids:
            try:
                client.delete_webpages(cleanup_ids)
            except Exception as exc:
                pytest.fail(
                    f"Cleanup of subscription/folder ids {cleanup_ids} "
                    f"failed: {exc}. Manual cleanup required."
                )
