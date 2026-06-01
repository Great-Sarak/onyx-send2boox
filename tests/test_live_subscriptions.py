"""Live round-trip tests for the Subscriptions module — folder + subscribe + list, OPML round-trip, unsubscribe.

Drives a real ``POST /api/1/subscribe/folder`` against push.boox.com,
catalog-searches for IEEE Spectrum (HAR-confirmed catalog match),
subscribes to it under the new folder, confirms via
``GET /api/1/subscribe/list``, then exercises the per-module unsubscribe
and OPML import / export methods (#31). All paths route through the
unified ``POST /api/1/webpage/bat/del`` endpoint for cleanup, per the
2026-05-31 finding that the misleadingly-named ``webpage/bat/del`` is the
single bulk-delete used for webpages, RSS feeds, OPDS feeds, and
folders.

These tests only need the Bearer JWT (``BOOX_TOKEN``) — the
``subscribe/*`` and ``rsses/*`` surfaces are both on /api/1 and accept
Bearer auth. No SyncGatewaySession required.

Skipped by default; run with:

    BOOX_RUN_LIVE_TESTS=1 \\
    BOOX_SECRETS_FILE=/path/to/secrets/boox.env \\
    pytest -m live tests/test_live_subscriptions.py -v

Added by #30 (Subscriptions module — catalog ops & folders).
Extended in #31 (OPML import/export + unsubscribe).
"""

import uuid
import xml.etree.ElementTree as ET

import pytest

import boox
from boox.errors import APIError
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


# --------------------------- unsubscribe (#31) -----------------------------


@pytest.mark.live
def test_unsubscribe_via_module_method(live_subscriptions_client):
    """Per-module ``unsubscribe`` removes a real user-sub record.

    Mirrors ``test_subscriptions_round_trip`` but the cleanup uses the
    new ``SubscriptionsClient.unsubscribe`` method instead of the legacy
    flat-client ``delete_webpages``. Confirms the unified
    ``webpage/bat/del`` endpoint accepts the same request shape from
    both surfaces (they share wire format by design — #31 just adds
    a clearer name at the call site).

    Final verification: re-list subscriptions, assert the user-sub _id
    is no longer present.
    """
    client = live_subscriptions_client
    folder_title = f"pytest-unsub-rss-{uuid.uuid4().hex[:8]}"
    folder_id = None
    user_sub_id = None

    try:
        folder_resp = client.subscriptions.create_folder(
            folder_title, FeedType.RSS
        )
        folder_id = folder_resp["data"]["_id"]

        search_resp = client.subscriptions.search_catalog(
            _IEEE_SPECTRUM_FEED_URL, FeedType.RSS
        )
        assert search_resp["data"]["count"] >= 1, (
            f"IEEE Spectrum no longer in catalog (count="
            f"{search_resp['data']['count']}); update the live-test URL."
        )
        feed_id = search_resp["data"]["results"][0]["_id"]

        sub_resp = client.subscriptions.subscribe(
            feed_id=feed_id, parent_folder_id=folder_id
        )
        user_sub_id = sub_resp["data"]["_id"]

        # Exercise the new per-module method specifically.
        unsub_resp = client.subscriptions.unsubscribe(user_sub_id)
        assert unsub_resp["result_code"] == 0, (
            f"unsubscribe returned non-zero: {unsub_resp}"
        )

        # Verify: the user-sub _id should no longer appear in the listing.
        list_resp = client.subscriptions.list_subscriptions(FeedType.RSS)
        all_ids = set()
        for entry in list_resp["data"]["results"]:
            all_ids.add(entry["_id"])
            for child in entry.get("children", []):
                all_ids.add(child["_id"])
        assert user_sub_id not in all_ids, (
            f"user_sub_id={user_sub_id!r} still present after unsubscribe."
        )

        # The unsubscribe consumed user_sub_id; only the folder needs cleanup.
        user_sub_id = None

    finally:
        cleanup_ids = [
            doc_id for doc_id in (user_sub_id, folder_id) if doc_id
        ]
        if cleanup_ids:
            try:
                client.subscriptions.unsubscribe_many(cleanup_ids)
            except Exception as exc:
                pytest.fail(
                    f"Cleanup of subscription/folder ids {cleanup_ids} "
                    f"failed: {exc}. Manual cleanup required."
                )


# --------------------------- OPML round-trip (#31) -------------------------


@pytest.mark.live
def test_opml_export_then_import_round_trip(live_subscriptions_client):
    """Export the user's subscriptions as OPML, then attempt a re-import.

    Flow:
    1. Subscribe to IEEE Spectrum under a fresh test folder so the
       export has something deterministic to contain.
    2. ``export_opml()`` and assert we got bytes back.
    3. Try to parse the bytes as XML. The 2026-05-31 captures show Boox
       sometimes returns HTML when the last import was bad — that's an
       upstream-data problem, not an issue with our wrapper, so we
       record the observation instead of failing the test outright.
    4. Unsubscribe via the per-module method.
    5. Attempt ``import_opml(<exported bytes>)``. Per the issue's
       fragility note (#31), this endpoint returned HTTP 500 twice in
       our HAR capture. We do not assume it works against the live API:
       a 500 ``APIError`` is recorded but does not fail the test. Any
       other failure mode does fail the test, so we'd notice a real
       regression in the request shape vs. an upstream bug.

    Cleanup is best-effort — the test folder and any rehydrated
    subscription get removed via the unified bulk-delete endpoint.
    """
    client = live_subscriptions_client
    folder_title = f"pytest-opml-rss-{uuid.uuid4().hex[:8]}"
    folder_id = None
    user_sub_id = None
    reimported_sub_ids: list[str] = []

    try:
        # 1) Subscribe so the export is non-empty.
        folder_resp = client.subscriptions.create_folder(
            folder_title, FeedType.RSS
        )
        folder_id = folder_resp["data"]["_id"]

        search_resp = client.subscriptions.search_catalog(
            _IEEE_SPECTRUM_FEED_URL, FeedType.RSS
        )
        feed_id = search_resp["data"]["results"][0]["_id"]
        sub_resp = client.subscriptions.subscribe(
            feed_id=feed_id, parent_folder_id=folder_id
        )
        user_sub_id = sub_resp["data"]["_id"]

        # 2) Export. Assert bytes returned.
        opml_bytes = client.subscriptions.export_opml()
        assert isinstance(opml_bytes, bytes), (
            f"export_opml returned {type(opml_bytes).__name__}, expected bytes"
        )
        assert len(opml_bytes) > 0, "export_opml returned empty bytes"

        # 3) Try to parse as XML. If the server returned HTML (the
        # 2026-05-31 captures show this happens after a bad upload),
        # don't fail — surface it as a warning so the test still
        # certifies the wire shape. We use ``Element.tag`` instead of a
        # strict OPML schema validator since OPML allows considerable
        # variation and Boox's exact dialect isn't documented.
        looked_like_xml = True
        contains_feed_url = False
        try:
            root = ET.fromstring(opml_bytes)
            # An OPML document's root element is <opml>; HTML round-tripped
            # back would have a different root.
            looked_like_xml = root.tag.lower() in {"opml"}
            contains_feed_url = (
                _IEEE_SPECTRUM_FEED_URL.encode("ascii") in opml_bytes
            )
        except ET.ParseError as exc:
            looked_like_xml = False
            # Record but don't fail — Boox-server-side, not our wrapper.
            print(
                f"NOTE: export_opml returned non-XML content "
                f"(ET.ParseError: {exc}); first 200 bytes: "
                f"{opml_bytes[:200]!r}"
            )

        if looked_like_xml and not contains_feed_url:
            print(
                f"NOTE: exported OPML didn't contain "
                f"{_IEEE_SPECTRUM_FEED_URL!r}; first 500 bytes: "
                f"{opml_bytes[:500]!r}"
            )

        # 4) Unsubscribe before the re-import so we can detect rehydration.
        client.subscriptions.unsubscribe(user_sub_id)
        original_user_sub_id = user_sub_id
        user_sub_id = None  # consumed

        # 5) Attempt re-import. May 500 (Boox upstream); record outcome.
        try:
            import_resp = client.subscriptions.import_opml(opml_bytes)
        except APIError as exc:
            # Documented fragility — accept HTTP 500 from the upstream
            # parser, but verify it's the expected failure mode rather
            # than silently swallowing all errors.
            print(
                f"NOTE: import_opml returned server error "
                f"(status_code={exc.status_code}, "
                f"result_code={exc.result_code}, "
                f"message={str(exc)!r}). Per #31 fragility note, this "
                f"is consistent with the 2026-05-31 HAR captures and "
                f"is treated as an observation, not a test failure."
            )
            assert exc.status_code == 500, (
                f"Expected upstream-parser 500 per #31 fragility note; "
                f"got status_code={exc.status_code}. Investigate."
            )
        else:
            # Happy path: import succeeded. Collect any rehydrated
            # subscription ids so cleanup can sweep them. Don't assert
            # exact-id equivalence — Boox may assign new user-sub _ids
            # on re-subscribe, and per #31 scope we're "consistent with
            # how Boox's UI handles duplicate-import", not strict
            # idempotence.
            assert isinstance(import_resp, dict)
            assert import_resp.get("result_code") == 0, (
                f"import_opml returned non-zero on 2xx: {import_resp}"
            )
            list_resp = client.subscriptions.list_subscriptions(FeedType.RSS)
            for entry in list_resp["data"]["results"]:
                for child in entry.get("children", []):
                    if (
                        child.get("url") == _IEEE_SPECTRUM_FEED_URL
                        and child["_id"] != original_user_sub_id
                    ):
                        reimported_sub_ids.append(child["_id"])

    finally:
        cleanup_ids = [
            doc_id for doc_id in (user_sub_id, folder_id) if doc_id
        ]
        cleanup_ids.extend(reimported_sub_ids)
        if cleanup_ids:
            try:
                client.subscriptions.unsubscribe_many(cleanup_ids)
            except Exception as exc:
                pytest.fail(
                    f"Cleanup of OPML round-trip ids {cleanup_ids} "
                    f"failed: {exc}. Manual cleanup required."
                )
