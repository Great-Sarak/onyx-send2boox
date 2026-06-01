"""Unit tests for ``boox.pushread``.

Covers the Pattern A ``PushRead`` subobject wired onto ``BooxClient`` (#29).

Endpoint coverage:

- ``GET  /api/1/webpage/list``    — HAR-confirmed.
- ``POST /api/1/webpage/url``     — HAR-confirmed (pushread-add-har-2026-05-31.json).
- ``POST /api/1/webpage/bat/del`` — HAR-confirmed.

Added by #29 (Phase 1 PushRead module — webpage CRUD).
"""

import json

import pytest

import boox
from boox import pushread
from .conftest import TEST_API_BASE, TEST_TOKEN


# --------------------------- Wiring (Pattern A) ----------------------------


def test_pushread_subobject_wired_on_client(boox_config):
    """``BooxClient.__init__`` wires the PushRead subobject."""
    client = boox.Boox(boox_config, skip_init=True)
    assert isinstance(client.pushread, pushread.PushRead)
    # Back-reference is the client itself.
    assert client.pushread._c is client


# --------------------------- list_webpages ---------------------------------


# HAR-confirmed response envelope shape (rss-subscribe-har-2026-05-31.json):
#   {"result_code":0, "list":[{...entry...}, ...],
#    "count":<int>, "message":"..."}
_LIST_SAMPLE_ENTRY = {
    "_id": "wp-fixture-id-1",
    "url": "https://example.com/article",
    "title": "Example article",
    "sourceType": 1,
    "updatedAt": "2026-05-31T19:45:00Z",
}


def test_list_webpages_default_params(mock_http, unit_client):
    """Default call hits ``webpage/list`` with the documented query string."""
    mock_http.get(
        f"{TEST_API_BASE}/webpage/list",
        json={"result_code": 0, "list": [_LIST_SAMPLE_ENTRY], "count": 1},
    )

    result = unit_client.pushread.list_webpages()

    assert result["result_code"] == 0
    assert result["list"] == [_LIST_SAMPLE_ENTRY]

    req = mock_http.calls[0].request
    assert req.method == "GET"
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    # ``responses`` exposes the resolved URL with query string.
    assert "limit=30" in req.url
    assert "page=1" in req.url
    assert "orderBy=-1" in req.url
    assert "sortBy=updatedAt" in req.url


def test_list_webpages_custom_paging(mock_http, unit_client):
    """``limit`` and ``page`` arguments thread through to the query string."""
    mock_http.get(
        f"{TEST_API_BASE}/webpage/list",
        json={"result_code": 0, "list": [], "count": 0},
    )

    unit_client.pushread.list_webpages(limit=50, page=3)

    url = mock_http.calls[0].request.url
    assert "limit=50" in url
    assert "page=3" in url
    # orderBy / sortBy stay fixed at the HAR-documented values regardless of
    # paging args — webpage listings are always newest-first by updatedAt.
    assert "orderBy=-1" in url
    assert "sortBy=updatedAt" in url


def test_list_webpages_empty_listing(mock_http, unit_client):
    """An empty inbox returns ``list=[]`` and ``count=0`` — not an error."""
    mock_http.get(
        f"{TEST_API_BASE}/webpage/list",
        json={"result_code": 0, "list": [], "count": 0},
    )

    result = unit_client.pushread.list_webpages()

    assert result["result_code"] == 0
    assert result["list"] == []
    assert result["count"] == 0


def test_list_webpages_error_envelope(mock_http, unit_client):
    """Non-zero ``result_code`` is surfaced verbatim (typed errors come with
    #28 — until then callers branch on ``result_code`` themselves)."""
    mock_http.get(
        f"{TEST_API_BASE}/webpage/list",
        json={"result_code": 4001, "message": "auth required", "list": []},
    )

    result = unit_client.pushread.list_webpages()

    assert result["result_code"] == 4001
    assert result["message"] == "auth required"


# --------------------------- delete_webpages -------------------------------


def test_delete_webpages_single_id(mock_http, unit_client):
    """Single-ID delete: body is ``{"ids": ["X"]}``, POST + Bearer."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    result = unit_client.pushread.delete_webpages(["wp-id-1"])

    assert result["result_code"] == 0
    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/webpage/bat/del")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"ids": ["wp-id-1"]}


def test_delete_webpages_batch(mock_http, unit_client):
    """Multiple ids fit in one call — body carries the full list."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.pushread.delete_webpages(["a", "b", "c"])

    assert json.loads(mock_http.calls[0].request.body) == {"ids": ["a", "b", "c"]}


def test_delete_webpages_accepts_tuple(mock_http, unit_client):
    """``Sequence[str]`` admits tuples — body is still a JSON list."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.pushread.delete_webpages(("a", "b"))

    assert json.loads(mock_http.calls[0].request.body) == {"ids": ["a", "b"]}


def test_delete_webpages_empty_list_still_posts(mock_http, unit_client):
    """Empty list isn't pre-flighted client-side; matches ``delete_files``.

    Documents chosen behavior so a future "optimize away empty calls" change
    is a deliberate decision rather than a silent behavior shift.
    """
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.pushread.delete_webpages([])

    assert len(mock_http.calls) == 1
    assert json.loads(mock_http.calls[0].request.body) == {"ids": []}


def test_delete_webpages_error_envelope(mock_http, unit_client):
    """Non-zero ``result_code`` surfaces (placeholder until #28 typed errors)."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    result = unit_client.pushread.delete_webpages(["x"])

    assert result["result_code"] == 1
    assert result["message"] == "boom"


# --------------------------- push_url --------------------------------------


# HAR-confirmed response envelope shape (pushread-add-har-2026-05-31.json
# entry 0): the cloud parses the URL server-side and returns the full
# webpage entry under ``data`` — ``_id`` is the handle used in
# ``delete_webpages`` for round-trip.
_PUSH_URL_SAMPLE_RESPONSE = {
    "result_code": 0,
    "data": {
        "_id": "6a1d016edeb4cd4b2109986d",
        "url": "https://www.terrygodier.com/the-boring-internet",
        "title": "The Boring Internet",
        "description": "A visual essay about what actually persists.",
        "sourceType": 1,
        "fileType": 1,
        "parent": None,
        "user": 387791,
        "createdAt": "2026-06-01T03:50:06.990Z",
        "updatedAt": "2026-06-01T03:50:06.990Z",
        "cbMsg": {"id": "6a1d016edeb4cd4b2109986d", "ok": True, "rev": "1-abc"},
    },
    "message": "SUCCESS",
    "tokenExpiredAt": 1787362074,
}


def test_push_url_default_top_level(mock_http, unit_client):
    """Default call (no parent folder): body is
    ``{"url": "...", "parentFolder": null}``, POST + Bearer."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/url",
        json=_PUSH_URL_SAMPLE_RESPONSE,
    )

    url = "https://www.terrygodier.com/the-boring-internet"
    result = unit_client.pushread.push_url(url)

    assert result["result_code"] == 0
    assert result["data"]["_id"] == "6a1d016edeb4cd4b2109986d"
    assert result["data"]["url"] == url

    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/webpage/url")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"url": url, "parentFolder": None}


def test_push_url_with_parent_folder(mock_http, unit_client):
    """Explicit ``parent_folder`` threads through to the JSON body verbatim.

    The HAR only confirms the ``null`` (top-level) case; we don't validate
    the folder-id shape client-side. Pass-through behavior is asserted here
    so a future "validate folder id" addition is a deliberate change.
    """
    mock_http.post(
        f"{TEST_API_BASE}/webpage/url",
        json=_PUSH_URL_SAMPLE_RESPONSE,
    )

    url = "https://example.com/some-article"
    unit_client.pushread.push_url(url, parent_folder="some-folder-id")

    assert json.loads(mock_http.calls[0].request.body) == {
        "url": url,
        "parentFolder": "some-folder-id",
    }


def test_push_url_error_envelope(mock_http, unit_client):
    """Non-zero ``result_code`` surfaces verbatim (placeholder until #28
    typed errors)."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/url",
        json={"result_code": 4001, "message": "auth required", "data": None},
    )

    result = unit_client.pushread.push_url("https://example.com/x")

    assert result["result_code"] == 4001
    assert result["message"] == "auth required"
