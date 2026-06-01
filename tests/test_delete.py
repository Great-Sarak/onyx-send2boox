"""Unit test suite — file & subscription delete.

Covers two endpoints:

- ``POST /api/1/push/message/batchDelete`` — BooxDrop file delete
  (single + batch). Body: ``{"ids": [...]}``. Bearer auth.
- ``POST /api/1/webpage/bat/del`` — the misleadingly-named unified
  bulk-delete that handles PushRead webpages **and** RSS / OPDS
  subscriptions (per 2026-05-31 finding). Same body shape.

Wrappers in ``boox.Boox``:

- ``delete_files(ids)`` → ``push/message/batchDelete``
- ``delete_webpages(ids)`` → ``webpage/bat/del`` (for webpages)
- ``unsubscribe(sub_ids)`` → ``webpage/bat/del`` (same endpoint, different
  caller intent — kept as separate method for readability).

Added by #8 (Unit test suite — file & subscription delete).
"""

import json

import pytest

from .conftest import TEST_API_BASE, TEST_TOKEN


# --------------------------- delete_files ----------------------------------


def test_delete_files_single_id(mock_http, unit_client):
    """Single-ID delete: body is ``{"ids": ["X"]}``, POST + Bearer."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    result = unit_client.delete_files(["file-id-1"])

    assert result["result_code"] == 0
    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"ids": ["file-id-1"]}


def test_delete_files_batch(mock_http, unit_client):
    """Multi-ID delete: body carries all ids in a single call."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.delete_files(["a", "b", "c"])

    body = json.loads(mock_http.calls[0].request.body)
    assert body == {"ids": ["a", "b", "c"]}


def test_delete_files_empty_list_still_posts(mock_http, unit_client):
    """Empty list: request fires (server-side handles the no-op).

    Documents chosen client-side behavior: we don't pre-flight or
    short-circuit on empty lists. Caller can guard if they want.
    """
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.delete_files([])

    assert len(mock_http.calls) == 1
    assert json.loads(mock_http.calls[0].request.body) == {"ids": []}


def test_delete_files_raises_api_error_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero result_code raises ``APIError`` (#28)."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.delete_files(["x"])
    assert excinfo.value.result_code == 1


# --------------------------- delete_webpages -------------------------------


def test_delete_webpages_routes_to_unified_endpoint(mock_http, unit_client):
    """delete_webpages hits ``webpage/bat/del`` with the same body shape."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    result = unit_client.delete_webpages(["webpage-id-1"])

    assert result["result_code"] == 0
    req = mock_http.calls[0].request
    assert req.url.endswith("/webpage/bat/del")
    assert req.method == "POST"
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"ids": ["webpage-id-1"]}


def test_delete_webpages_batch(mock_http, unit_client):
    """Multi-ID delete on the unified endpoint."""
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.delete_webpages(["w1", "w2", "w3"])

    assert json.loads(mock_http.calls[0].request.body) == {"ids": ["w1", "w2", "w3"]}


# --------------------------- unsubscribe (alias) ---------------------------


def test_unsubscribe_routes_to_webpage_bat_del(mock_http, unit_client):
    """``unsubscribe`` is an intent-named alias for ``delete_webpages``.

    Both methods hit the same misleadingly-named ``webpage/bat/del``
    endpoint that Boox uses uniformly for webpages, RSS, and OPDS subs.
    Asserting the routing here means callers can use whichever method
    name makes their intent clearer at the call site.
    """
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.unsubscribe(["sub-id-1"])

    req = mock_http.calls[0].request
    assert req.url.endswith("/webpage/bat/del")
    assert json.loads(req.body) == {"ids": ["sub-id-1"]}


# --------------------------- Endpoints are distinct ------------------------


def test_delete_files_and_delete_webpages_hit_different_endpoints(
    mock_http, unit_client
):
    """``delete_files`` and ``delete_webpages`` should NOT collapse to
    the same endpoint — they hit different paths even though the body
    shape is identical."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )
    mock_http.post(
        f"{TEST_API_BASE}/webpage/bat/del",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.delete_files(["f"])
    unit_client.delete_webpages(["w"])

    urls = [c.request.url for c in mock_http.calls]
    assert any("push/message/batchDelete" in u for u in urls)
    assert any("webpage/bat/del" in u for u in urls)
