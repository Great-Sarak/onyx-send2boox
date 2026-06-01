"""Unit tests for ``boox.subscriptions``.

Covers the Pattern A ``SubscriptionsClient`` subobject wired onto
``BooxClient`` (#30, #31).

Endpoint coverage (all HAR-confirmed):

- ``GET  /api/1/subscribe/list``         — HAR-confirmed (rss-subscribe-har-2026-05-31.json).
- ``POST /api/1/subscribe/folder``       — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``POST /api/1/subscribe/sub``          — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``GET  /api/1/rsses/public/search``    — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``GET  /api/1/rsses/public/recommend`` — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``GET  /api/1/rsses/one/detail``       — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``POST /api/1/rsses/url/content``      — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``POST /api/1/webpage/bat/del``        — HAR-confirmed (rss-subscribe-har-2026-05-31.json, #31).
- ``POST /api/1/rsses/opml/export``      — HAR-confirmed (push-boox-har-2026-05-31.json, #31).
- ``GET  /uploads/feed/export/<uuid>.opml`` — HAR-confirmed follow-up (#31).
- ``POST /api/1/rsses/opml/import``      — HAR-confirmed (rss-subscribe-har-2026-05-31.json, #31; observed 500).

Added by #30 (Phase 2 Subscriptions module — catalog ops & folders).
Extended in #31 (OPML import/export + unsubscribe).
"""

import json

import pytest

import boox
from boox import subscriptions
from boox.subscriptions import FeedType, SubscriptionsClient
from .conftest import TEST_API_BASE, TEST_CLOUD, TEST_TOKEN


# --------------------------- Wiring (Pattern A) ----------------------------


def test_subscriptions_subobject_wired_on_client(boox_config):
    """``BooxClient.__init__`` wires the SubscriptionsClient subobject."""
    client = boox.Boox(boox_config, skip_init=True)
    assert isinstance(client.subscriptions, SubscriptionsClient)
    # Back-reference is the client itself.
    assert client.subscriptions._c is client


# --------------------------- FeedType --------------------------------------


def test_feedtype_values_match_har_capture():
    """RSS=0 and OPDS=2 are the HAR-confirmed sourceType values."""
    assert int(FeedType.RSS) == 0
    assert int(FeedType.OPDS) == 2


def test_feedtype_is_intenum_for_serialization():
    """``IntEnum`` so callers can pass ``FeedType.RSS`` or ``0`` interchangeably."""
    from enum import IntEnum

    assert issubclass(FeedType, IntEnum)
    # int() coercion matches the raw integer — what
    # ``api_call``'s param/body serialization relies on.
    assert int(FeedType.RSS) == 0
    assert int(FeedType.OPDS) == 2


# --------------------------- list_subscriptions ----------------------------


# HAR-confirmed response envelope shape (rss-subscribe-har-2026-05-31.json):
#   {"result_code":0, "data":{"count":<int>, "results":[...]}, "message":"...",
#    "tokenExpiredAt":<int>}
_LIST_SAMPLE_ENTRY = {
    "_id": "65a20b1195d77268e0574cda",
    "url": "https://www.atlassian.com/blog/rss",
    "title": "Work Life by Atlassian",
    "sourceType": 0,
    "parent": "65a20b0c95d77268e0574cd6",
    "updatedAt": "2024-01-13T04:01:21.250Z",
}


def test_list_subscriptions_rss(mock_http, unit_client):
    """Default call hits ``subscribe/list`` with the HAR query string."""
    mock_http.get(
        f"{TEST_API_BASE}/subscribe/list",
        json={
            "result_code": 0,
            "data": {"count": 1, "results": [_LIST_SAMPLE_ENTRY]},
        },
    )

    result = unit_client.subscriptions.list_subscriptions(FeedType.RSS)

    assert result["result_code"] == 0
    assert result["data"]["results"] == [_LIST_SAMPLE_ENTRY]

    req = mock_http.calls[0].request
    assert req.method == "GET"
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    # Query string carries all four HAR-documented params, defaults match capture.
    assert "sourceType=0" in req.url
    assert "limit=100000" in req.url
    assert "page=1" in req.url
    assert "sortBy=updatedAt" in req.url


def test_list_subscriptions_opds_type_filter(mock_http, unit_client):
    """``sourceType=2`` threads through for OPDS subscriptions."""
    mock_http.get(
        f"{TEST_API_BASE}/subscribe/list",
        json={"result_code": 0, "data": {"count": 0, "results": []}},
    )

    unit_client.subscriptions.list_subscriptions(FeedType.OPDS)

    assert "sourceType=2" in mock_http.calls[0].request.url


def test_list_subscriptions_accepts_raw_int(mock_http, unit_client):
    """Raw int ``source_type`` (e.g. ``0``) works as well as ``FeedType.RSS``."""
    mock_http.get(
        f"{TEST_API_BASE}/subscribe/list",
        json={"result_code": 0, "data": {"count": 0, "results": []}},
    )

    unit_client.subscriptions.list_subscriptions(0)

    assert "sourceType=0" in mock_http.calls[0].request.url


def test_list_subscriptions_custom_paging(mock_http, unit_client):
    """``limit`` and ``page`` overrides thread through to the query string."""
    mock_http.get(
        f"{TEST_API_BASE}/subscribe/list",
        json={"result_code": 0, "data": {"count": 0, "results": []}},
    )

    unit_client.subscriptions.list_subscriptions(FeedType.RSS, limit=50, page=3)

    url = mock_http.calls[0].request.url
    assert "limit=50" in url
    assert "page=3" in url


def test_list_subscriptions_empty_listing(mock_http, unit_client):
    """An empty subscription set returns ``count=0`` — not an error."""
    mock_http.get(
        f"{TEST_API_BASE}/subscribe/list",
        json={"result_code": 0, "data": {"count": 0, "results": []}},
    )

    result = unit_client.subscriptions.list_subscriptions(FeedType.RSS)

    assert result["result_code"] == 0
    assert result["data"]["count"] == 0
    assert result["data"]["results"] == []


def test_list_subscriptions_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.get(
        f"{TEST_API_BASE}/subscribe/list",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.list_subscriptions(FeedType.RSS)
    assert excinfo.value.result_code == 4001


# --------------------------- create_folder ---------------------------------


# HAR-confirmed response envelope (push-boox-har-2026-05-31.json):
# the new folder doc with its ``_id`` returned under ``data``.
_FOLDER_SAMPLE_RESPONSE = {
    "result_code": 0,
    "data": {
        "_id": "6a1c82baeef3164b0adb744e",
        "title": "Test Group",
        "sourceType": 0,
        "parent": None,
        "fileType": 0,
        "createdAt": "2026-05-31T18:49:30.364Z",
        "updatedAt": "2026-05-31T18:49:30.364Z",
    },
    "message": "SUCCESS",
}


def test_create_folder_rss_shape(mock_http, unit_client):
    """Body is ``{"title": "...", "sourceType": 0}`` for RSS folder create."""
    mock_http.post(
        f"{TEST_API_BASE}/subscribe/folder",
        json=_FOLDER_SAMPLE_RESPONSE,
    )

    result = unit_client.subscriptions.create_folder("Test Group", FeedType.RSS)

    assert result["data"]["_id"] == "6a1c82baeef3164b0adb744e"

    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/subscribe/folder")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"title": "Test Group", "sourceType": 0}


def test_create_folder_opds_shape(mock_http, unit_client):
    """Body carries ``sourceType=2`` for OPDS folder create."""
    mock_http.post(
        f"{TEST_API_BASE}/subscribe/folder",
        json=_FOLDER_SAMPLE_RESPONSE,
    )

    unit_client.subscriptions.create_folder("Library", FeedType.OPDS)

    assert json.loads(mock_http.calls[0].request.body) == {
        "title": "Library",
        "sourceType": 2,
    }


def test_create_folder_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/subscribe/folder",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.create_folder("x", FeedType.RSS)
    assert excinfo.value.result_code == 1


# --------------------------- subscribe -------------------------------------


# HAR-confirmed response envelope (push-boox-har-2026-05-31.json):
# the new user-sub record with the ``_id`` that ``unsubscribe`` consumes.
_SUBSCRIBE_SAMPLE_RESPONSE = {
    "result_code": 0,
    "data": {
        "_id": "6a1c835002807c4b19b6c7f2",
        "parent": "6a1c82baeef3164b0adb744e",
        "subFrom": "62ec90210f9f61452dcc6ddd",
        "fromPublicFeed": 1,
        "subState": 1,
        "title": "IEEE Spectrum",
        "url": "https://spectrum.ieee.org/feeds/feed.rss",
        "sourceType": 0,
    },
    "message": "SUCCESS",
}


def test_subscribe_body_shape(mock_http, unit_client):
    """Body is ``{"parent": <folder_id>, "id": <feed_id>}`` — HAR-confirmed."""
    mock_http.post(
        f"{TEST_API_BASE}/subscribe/sub",
        json=_SUBSCRIBE_SAMPLE_RESPONSE,
    )

    result = unit_client.subscriptions.subscribe(
        feed_id="62ec90210f9f61452dcc6ddd",
        parent_folder_id="6a1c82baeef3164b0adb744e",
    )

    assert result["data"]["_id"] == "6a1c835002807c4b19b6c7f2"

    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/subscribe/sub")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {
        "parent": "6a1c82baeef3164b0adb744e",
        "id": "62ec90210f9f61452dcc6ddd",
    }


def test_subscribe_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/subscribe/sub",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.subscribe(feed_id="x", parent_folder_id="y")
    assert excinfo.value.result_code == 1


# --------------------------- search_catalog --------------------------------


# HAR-confirmed (push-boox-har-2026-05-31.json,
# text=https://spectrum.ieee.org/feeds/feed.rss&sourceType=0):
#   {"result_code":0, "data":{"count":1, "results":[<feed_doc>]}, "message":"SUCCESS"}
_SEARCH_HIT_RESPONSE = {
    "result_code": 0,
    "data": {
        "count": 1,
        "results": [
            {
                "_id": "62ec90210f9f61452dcc6ddd",
                "title": "IEEE Spectrum",
                "url": "https://spectrum.ieee.org/feeds/feed.rss",
                "sourceType": 0,
                "publicFeed": 1,
            }
        ],
    },
    "message": "SUCCESS",
}


def test_search_catalog_match_returns_results(mock_http, unit_client):
    """Hit: ``count=1`` returned, ``_id`` extractable for ``subscribe`` (#30 AC)."""
    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/search",
        json=_SEARCH_HIT_RESPONSE,
    )

    result = unit_client.subscriptions.search_catalog(
        "https://spectrum.ieee.org/feeds/feed.rss", FeedType.RSS
    )

    assert result["data"]["count"] == 1
    assert result["data"]["results"][0]["_id"] == "62ec90210f9f61452dcc6ddd"

    req = mock_http.calls[0].request
    assert req.method == "GET"
    # ``responses`` doesn't decode the URL — assert on quoted form.
    assert "sourceType=0" in req.url
    # ``requests`` percent-encodes the ``text`` query param.
    assert "spectrum.ieee.org" in req.url


def test_search_catalog_miss_returns_empty(mock_http, unit_client):
    """Miss: catalog returns ``count=0`` — not an error (#30 AC).

    Mirrors the captured behavior for a non-catalog URL — confirming the
    catalog-only limitation documented on the module.
    """
    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/search",
        json={
            "result_code": 0,
            "data": {"count": 0, "results": []},
            "message": "SUCCESS",
        },
    )

    result = unit_client.subscriptions.search_catalog(
        "https://example.com/not-in-catalog/feed.rss", FeedType.RSS
    )

    assert result["data"]["count"] == 0
    assert result["data"]["results"] == []


def test_search_catalog_opds_type(mock_http, unit_client):
    """``sourceType=2`` threads through for OPDS catalog searches."""
    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/search",
        json={"result_code": 0, "data": {"count": 0, "results": []}},
    )

    unit_client.subscriptions.search_catalog("anything", FeedType.OPDS)

    assert "sourceType=2" in mock_http.calls[0].request.url


def test_search_catalog_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/search",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.search_catalog("x", FeedType.RSS)
    assert excinfo.value.result_code == 4001


# --------------------------- recommended -----------------------------------


def test_recommended_rss(mock_http, unit_client):
    """``GET rsses/public/recommend?sourceType=0`` returns curated RSS list."""
    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/recommend",
        json={
            "result_code": 0,
            "data": {
                "count": 1,
                "results": [
                    {"_id": "62eb87b40f9f61452dcc61b5", "title": "Group A"}
                ],
            },
            "message": "SUCCESS",
        },
    )

    result = unit_client.subscriptions.recommended(FeedType.RSS)

    assert result["data"]["count"] == 1
    assert "sourceType=0" in mock_http.calls[0].request.url


def test_recommended_opds_type(mock_http, unit_client):
    """``sourceType=2`` threads through for OPDS recommendations."""
    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/recommend",
        json={"result_code": 0, "data": {"count": 0, "results": []}},
    )

    unit_client.subscriptions.recommended(FeedType.OPDS)

    assert "sourceType=2" in mock_http.calls[0].request.url


def test_recommended_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.get(
        f"{TEST_API_BASE}/rsses/public/recommend",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.recommended(FeedType.RSS)
    assert excinfo.value.result_code == 4001


# --------------------------- feed_detail -----------------------------------


def test_feed_detail_passes_id_param(mock_http, unit_client):
    """``GET rsses/one/detail?id=<feed_id>`` returns the catalog feed doc."""
    mock_http.get(
        f"{TEST_API_BASE}/rsses/one/detail",
        json={
            "result_code": 0,
            "data": {
                "_id": "62eb87b40f9f61452dcc61b5",
                "title": "Featured Sources",
                "children": [],
            },
            "message": "SUCCESS",
        },
    )

    result = unit_client.subscriptions.feed_detail("62eb87b40f9f61452dcc61b5")

    assert result["data"]["_id"] == "62eb87b40f9f61452dcc61b5"
    req = mock_http.calls[0].request
    assert req.method == "GET"
    assert "id=62eb87b40f9f61452dcc61b5" in req.url


def test_feed_detail_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.get(
        f"{TEST_API_BASE}/rsses/one/detail",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.feed_detail("x")
    assert excinfo.value.result_code == 1


# --------------------------- preview_feed_url ------------------------------


def test_preview_feed_url_body_shape(mock_http, unit_client):
    """Body matches the HAR shape verbatim — limit / page / sortBy preserved.

    The captured web flow always sends these list-style fields even though
    the endpoint is a URL preview; we mirror it to stay HAR-grounded.
    HAR source: ``push-boox-har-2026-05-31.json`` entry
    ``POST /api/1/rsses/url/content`` with body
    ``{"limit":100000,"page":1,"sourceType":0,"sortBy":"updatedAt","url":"..."}``.
    """
    mock_http.post(
        f"{TEST_API_BASE}/rsses/url/content",
        json={
            "result_code": 0,
            "data": {"count": 0, "fileCount": 0, "folderCount": 0, "results": []},
            "message": "SUCCESS",
        },
    )

    url = "https://www.atlassian.com/blog/rss"
    unit_client.subscriptions.preview_feed_url(url, FeedType.RSS)

    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/rsses/url/content")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {
        "limit": 100000,
        "page": 1,
        "sourceType": 0,
        "sortBy": "updatedAt",
        "url": url,
    }


def test_preview_feed_url_opds_type(mock_http, unit_client):
    """``sourceType=2`` threads into the body for OPDS previews."""
    mock_http.post(
        f"{TEST_API_BASE}/rsses/url/content",
        json={
            "result_code": 0,
            "data": {"count": 0, "results": []},
        },
    )

    unit_client.subscriptions.preview_feed_url(
        "https://library.oapen.org/opds", FeedType.OPDS
    )

    body = json.loads(mock_http.calls[0].request.body)
    assert body["sourceType"] == 2
    assert body["url"] == "https://library.oapen.org/opds"


def test_preview_feed_url_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/rsses/url/content",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.preview_feed_url("https://x", FeedType.RSS)
    assert excinfo.value.result_code == 4001


# --------------------------- unsubscribe / unsubscribe_many ---------------


# HAR-confirmed (rss-subscribe-har-2026-05-31.json):
#   POST https://push.boox.com/api/1/webpage/bat/del
#   body  {"ids":["6a1c9ce3961d4d4b17564650"]}
#   resp  {"result_code":0,"data":"ok","message":"SUCCESS","tokenExpiredAt":...}
_BAT_DEL_OK_RESPONSE = {
    "result_code": 0,
    "data": "ok",
    "message": "SUCCESS",
}


def test_unsubscribe_single_body_shape(mock_http, unit_client):
    """``unsubscribe(sub_id)`` posts ``{"ids": [<sub_id>]}`` to ``webpage/bat/del``."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json=_BAT_DEL_OK_RESPONSE,
    )

    result = unit_client.subscriptions.unsubscribe("6a1c9ce3961d4d4b17564650")

    assert result == _BAT_DEL_OK_RESPONSE

    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/webpage/bat/del")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"ids": ["6a1c9ce3961d4d4b17564650"]}


def test_unsubscribe_many_body_shape(mock_http, unit_client):
    """``unsubscribe_many(ids)`` threads all ids into one ``webpage/bat/del`` body."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json=_BAT_DEL_OK_RESPONSE,
    )

    ids = ["aaa", "bbb", "ccc"]
    result = unit_client.subscriptions.unsubscribe_many(ids)

    assert result == _BAT_DEL_OK_RESPONSE
    assert json.loads(mock_http.calls[0].request.body) == {"ids": ids}


def test_unsubscribe_many_empty_list_is_client_side_noop(mock_http, unit_client):
    """Empty ``sub_ids`` short-circuits to ``None`` with no network call.

    Documented behavior — see the docstring on ``unsubscribe_many``.
    The endpoint hasn't been HAR-captured with an empty ``ids`` array, so
    rather than guess the server's handling we return ``None`` so callers
    using filter expressions can no-op cleanly.
    """
    # No mock_http.post registered: any outgoing request would fail the
    # responses fixture's ConnectionError check, asserting the no-op.
    result = unit_client.subscriptions.unsubscribe_many([])

    assert result is None
    assert len(mock_http.calls) == 0


def test_unsubscribe_many_accepts_arbitrary_sequence(mock_http, unit_client):
    """``Sequence`` typing — tuples / generators work, body still serializes."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json=_BAT_DEL_OK_RESPONSE,
    )

    unit_client.subscriptions.unsubscribe_many(("a", "b"))

    assert json.loads(mock_http.calls[0].request.body) == {"ids": ["a", "b"]}


def test_unsubscribe_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.unsubscribe("x")
    assert excinfo.value.result_code == 1


# --------------------------- export_opml -----------------------------------


# HAR-confirmed (push-boox-har-2026-05-31.json / rss-subscribe HAR):
# 1. POST /api/1/rsses/opml/export -> {"data": "/uploads/feed/export/<uuid>.opml", ...}
# 2. GET <cloud><relative_url>     -> OPML bytes (no Authorization header in capture).
_EXPORT_RELATIVE_URL = "/uploads/feed/export/dfac64fc-080a-4f22-9f4a-7a9ce08b4dc7.opml"
_EXPORT_FILE_URL = f"https://{TEST_CLOUD}{_EXPORT_RELATIVE_URL}"

# Compact but valid OPML so tests can assert on real content shape.
_SAMPLE_OPML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<opml version="2.0"><head><title>Subscriptions</title></head>'
    b'<body><outline text="IEEE Spectrum" type="rss" '
    b'xmlUrl="https://spectrum.ieee.org/feeds/feed.rss"/></body></opml>'
)


def test_export_opml_returns_fetched_file_bytes(mock_http, unit_client):
    """Two-step flow: POST export → GET file → return ``bytes``."""
    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/export",
        json={
            "result_code": 0,
            "data": _EXPORT_RELATIVE_URL,
            "message": "SUCCESS",
        },
    )
    mock_http.get(_EXPORT_FILE_URL, body=_SAMPLE_OPML, status=200)

    result = unit_client.subscriptions.export_opml()

    assert result == _SAMPLE_OPML

    # First call is the POST to /rsses/opml/export.
    post_req = mock_http.calls[0].request
    assert post_req.method == "POST"
    assert post_req.url.endswith("/rsses/opml/export")
    assert post_req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    # HAR-confirmed empty body — matches the captured Content-Length: 0.
    assert not post_req.body

    # Second call is the GET to the relative URL composed with the cloud.
    get_req = mock_http.calls[1].request
    assert get_req.method == "GET"
    assert get_req.url == _EXPORT_FILE_URL
    # Captured browser GET sent no Authorization header — we mirror that.
    assert "Authorization" not in get_req.headers


def test_export_opml_post_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` on the export-generation POST raises ``APIError``."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/export",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.export_opml()
    assert excinfo.value.result_code == 4001


def test_export_opml_propagates_http_error_on_file_fetch(
    mock_http, unit_client
):
    """Non-2xx on the static-file GET surfaces via ``raise_for_status``.

    The OPML file URL is static content (no result-code envelope), so we
    fall back to the transport-layer error rather than the typed
    ``BooxError`` path used by JSON endpoints.
    """
    import requests as _requests

    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/export",
        json={
            "result_code": 0,
            "data": _EXPORT_RELATIVE_URL,
            "message": "SUCCESS",
        },
    )
    mock_http.get(_EXPORT_FILE_URL, body="not found", status=404)

    with pytest.raises(_requests.HTTPError):
        unit_client.subscriptions.export_opml()


# --------------------------- import_opml -----------------------------------


def _multipart_body_text(req):
    """Decode the ``responses``-captured multipart body to str for assertions.

    ``responses`` (built on urllib3) sends the multipart body as bytes;
    decode to UTF-8 with replacement so byte-level assertions on the
    headers and field name still work even if the file content isn't
    decodable.
    """
    body = req.body
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", errors="replace")
    return body


def test_import_opml_multipart_shape(mock_http, unit_client):
    """Multipart body carries a ``file`` field with the supplied bytes."""
    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/import",
        json={
            "result_code": 0,
            "data": {"imported": 1, "failed": 0},
            "message": "SUCCESS",
        },
    )

    result = unit_client.subscriptions.import_opml(
        _SAMPLE_OPML, filename="round-trip.opml"
    )

    assert result["result_code"] == 0

    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/rsses/opml/import")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    # multipart/form-data with the same boundary urllib3 generated.
    content_type = req.headers.get("Content-Type", "")
    assert content_type.startswith("multipart/form-data"), content_type
    assert "boundary=" in content_type

    body_text = _multipart_body_text(req)
    # Field name and supplied filename appear in the part header.
    assert 'name="file"' in body_text
    assert 'filename="round-trip.opml"' in body_text
    # File content type mirrors the captured browser request.
    assert "text/x-opml+xml" in body_text
    # File bytes themselves are present in the multipart body.
    assert _SAMPLE_OPML.decode("utf-8") in body_text


def test_import_opml_default_filename(mock_http, unit_client):
    """``filename`` defaults to ``subscriptions.opml`` when caller omits it."""
    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/import",
        json={"result_code": 0, "data": None, "message": "SUCCESS"},
    )

    unit_client.subscriptions.import_opml(_SAMPLE_OPML)

    body_text = _multipart_body_text(mock_http.calls[0].request)
    assert 'filename="subscriptions.opml"' in body_text


def test_import_opml_surfaces_500_parse_error_as_api_error(
    mock_http, unit_client
):
    """HTTP 500 + ``{"result_code":1, "message": "Attribute without value..."}`` raises ``APIError``.

    Mirrors the failure observed in the 2026-05-31 capture
    (``rss-subscribe-har-2026-05-31.json`` entries 87/88). The
    upstream-parser message is preserved on the exception so callers
    can show what Boox actually said.
    """
    from boox.errors import APIError

    parser_message = (
        "Attribute without value\nLine: 6\nColumn: 19\nChar: s"
    )
    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/import",
        json={"result_code": 1, "message": parser_message, "data": None},
        status=500,
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.import_opml(b"<not-real-opml/>")

    assert excinfo.value.status_code == 500
    assert excinfo.value.result_code == 1
    # The upstream parser message is preserved verbatim on the response
    # body so debugging callers can see exactly what Boox returned.
    # response_body is the raw JSON text — parse it and assert the
    # message round-trips with its real newlines intact.
    assert excinfo.value.response_body is not None
    assert json.loads(excinfo.value.response_body)["message"] == parser_message


def test_import_opml_raises_api_error_on_2xx_nonzero_result_code(
    mock_http, unit_client
):
    """A 200 OK with non-zero ``result_code`` still raises ``APIError``."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/rsses/opml/import",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.subscriptions.import_opml(_SAMPLE_OPML)
    assert excinfo.value.result_code == 4001
