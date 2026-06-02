"""Unit tests for ``boox.sync`` — the Couchbase Sync Gateway primitives.

Covers the Pattern A ``SyncClient`` subobject wired onto ``BooxClient``
(#34). All five wire shapes are HAR-grounded against
``tools/boox/captures/`` (entry-count audit per
``memory/feedback_dispatch_har_pre_confirmed_check.md``).

Endpoint coverage:

- ``GET  /neocloud/_changes``                — HAR-confirmed.
- ``POST /neocloud/_bulk_get``               — HAR-confirmed (406 fallback).
- ``GET  /neocloud/<doc_id>?open_revs=...``  — HAR-confirmed (bulk_get fallback).
- ``POST /neocloud/_bulk_docs``              — HAR-confirmed.
- ``POST /neocloud/_revs_diff``              — HAR-confirmed.
- ``GET  /neocloud/_local/<key>``            — HAR-confirmed.
- ``PUT  /neocloud/_local/<key>``            — HAR-confirmed.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import responses

import boox
from boox.errors import AuthError, NotFoundError
from boox.sync import ChangesResult, SyncClient, SyncProtocolError
from .conftest import TEST_NEOCLOUD_BASE, TEST_SYNC_TOKEN


# --------------------------- Pattern A wiring ------------------------------


def test_sync_subobject_wired_on_client(boox_config):
    """``BooxClient.__init__`` wires the SyncClient subobject."""
    client = boox.Boox(boox_config, skip_init=True)
    assert isinstance(client.sync, SyncClient)
    assert client.sync._c is client


# --------------------------- helpers ---------------------------------------


def _qs(url):
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def _assert_cookie_only_auth(req):
    """Every /neocloud/* request must carry the cookie, no Bearer header."""
    cookie = req.headers.get("Cookie", "")
    assert f"SyncGatewaySession={TEST_SYNC_TOKEN}" in cookie
    assert "Authorization" not in req.headers


# --------------------------- _changes --------------------------------------


def test_changes_happy_path(mock_http, unit_client):
    """``changes`` issues the canonical bychannel-filtered GET."""
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_changes",
        json={
            "results": [
                {"seq": "1::100", "id": "doc-a", "changes": [{"rev": "1-aaa"}]},
                {"seq": "1::101", "id": "doc-b", "changes": [{"rev": "1-bbb"}]},
            ],
            "last_seq": "1::101",
        },
    )

    result = unit_client.sync.changes(
        "user-uid-MESSAGE", since="1::50", limit=500
    )

    assert isinstance(result, ChangesResult)
    assert result.last_seq == "1::101"
    records = list(result)
    assert [r["id"] for r in records] == ["doc-a", "doc-b"]

    req = mock_http.calls[0].request
    assert req.method == "GET"
    _assert_cookie_only_auth(req)
    qs = _qs(req.url)
    assert qs["style"] == ["all_docs"]
    assert qs["filter"] == ["sync_gateway/bychannel"]
    assert qs["channels"] == ["user-uid-MESSAGE"]
    assert qs["since"] == ["1::50"]
    assert qs["limit"] == ["500"]
    assert "feed" not in qs


def test_changes_longpoll_sets_feed_and_heartbeat(mock_http, unit_client):
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_changes",
        json={"results": [], "last_seq": "1::101"},
    )

    unit_client.sync.changes(
        "channel-x", longpoll=True, heartbeat_ms=10000
    )

    qs = _qs(mock_http.calls[0].request.url)
    assert qs["feed"] == ["longpoll"]
    assert qs["heartbeat"] == ["10000"]


def test_changes_iterator_shape(mock_http, unit_client):
    """``ChangesResult`` is iterable — len + iter both work."""
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_changes",
        json={
            "results": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "last_seq": "1::3",
        },
    )

    result = unit_client.sync.changes("ch")
    # Iterating produces the records one at a time (generator-shaped).
    it = iter(result)
    assert next(it)["id"] == "a"
    assert next(it)["id"] == "b"
    # last_seq is still queryable after partial iteration.
    assert result.last_seq == "1::3"
    assert len(result) == 3


def test_changes_no_since_omits_param(mock_http, unit_client):
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_changes",
        json={"results": [], "last_seq": "0"},
    )
    unit_client.sync.changes("ch")
    qs = _qs(mock_http.calls[0].request.url)
    assert "since" not in qs


# --------------------------- _bulk_get -------------------------------------


def test_bulk_get_happy_path_json(mock_http, unit_client):
    """200 JSON response — PouchDB-style ``results[].docs[]`` flattened."""
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_get",
        json={
            "results": [
                {"id": "doc-a", "docs": [{"ok": {"_id": "doc-a", "_rev": "1-aaa"}}]},
                {"id": "doc-b", "docs": [{"ok": {"_id": "doc-b", "_rev": "1-bbb"}}]},
            ]
        },
        status=200,
    )

    out = unit_client.sync.bulk_get([
        {"id": "doc-a", "rev": "1-aaa"},
        {"id": "doc-b", "rev": "1-bbb"},
    ])

    assert [e["ok"]["_id"] for e in out] == ["doc-a", "doc-b"]
    req = mock_http.calls[0].request
    _assert_cookie_only_auth(req)
    qs = _qs(req.url)
    assert qs["revs"] == ["true"]
    assert qs["latest"] == ["true"]
    body = json.loads(req.body)
    assert body == {
        "docs": [
            {"id": "doc-a", "rev": "1-aaa"},
            {"id": "doc-b", "rev": "1-bbb"},
        ]
    }


def test_bulk_get_406_falls_back_to_per_doc_get(mock_http, unit_client):
    """406 multipart response triggers per-doc ``open_revs`` GETs."""
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_get",
        json={"error": "Not Acceptable", "reason": "Response is multipart"},
        status=406,
    )
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/doc-a",
        json=[{"ok": {"_id": "doc-a", "_rev": "1-aaa", "value": 1}}],
        status=200,
    )
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/doc-b",
        json=[{"ok": {"_id": "doc-b", "_rev": "1-bbb", "value": 2}}],
        status=200,
    )

    out = unit_client.sync.bulk_get([
        {"id": "doc-a", "rev": "1-aaa"},
        {"id": "doc-b", "rev": "1-bbb"},
    ])

    assert [e["ok"]["_id"] for e in out] == ["doc-a", "doc-b"]
    # 1 POST + 2 per-doc GETs = 3 calls.
    assert len(mock_http.calls) == 3
    fallback_urls = [c.request.url for c in mock_http.calls[1:]]
    for url in fallback_urls:
        _assert_cookie_only_auth(
            next(c.request for c in mock_http.calls if c.request.url == url)
        )
        qs = _qs(url)
        assert qs["revs"] == ["true"]
        assert qs["latest"] == ["true"]
        # open_revs is a JSON-encoded single-element array.
        open_revs = json.loads(qs["open_revs"][0])
        assert isinstance(open_revs, list) and len(open_revs) == 1


def test_bulk_get_fallback_preserves_missing_entries(mock_http, unit_client):
    """A ``missing`` entry from the per-doc GET surfaces with ``id`` filled in."""
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_get",
        json={"error": "Not Acceptable", "reason": "Response is multipart"},
        status=406,
    )
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/doc-a",
        json=[{"missing": "9-doesnotexist"}],
        status=200,
    )

    out = unit_client.sync.bulk_get([{"id": "doc-a", "rev": "9-doesnotexist"}])
    assert out == [{"missing": "9-doesnotexist", "id": "doc-a"}]


def test_bulk_get_fallback_url_encodes_hash_in_doc_id(mock_http, unit_client):
    """READER_LIBRARY doc_ids are ``<user>#<docid>`` — the ``#`` must be
    percent-encoded as ``%23`` in the per-doc fallback path, otherwise
    HTTP fragment semantics drop the tail and the server receives a GET
    for ``<user>`` only. Regression for #68.
    """
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_get",
        json={"error": "Not Acceptable", "reason": "Response is multipart"},
        status=406,
    )
    encoded_id = "user-uid#book-uuid"
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/user-uid%23book-uuid",
        json=[{"ok": {"_id": encoded_id, "_rev": "1-aaa", "value": 1}}],
        status=200,
    )

    out = unit_client.sync.bulk_get([{"id": encoded_id, "rev": "1-aaa"}])

    assert out == [{"ok": {"_id": encoded_id, "_rev": "1-aaa", "value": 1}}]
    # The actual fired URL (after responses unescapes for matching) must
    # contain the encoded ``%23`` form. responses' call.request.url shows
    # the literal URL the requests-library serialized.
    fallback_url = mock_http.calls[1].request.url
    assert "%23" in fallback_url, f"Expected %23-encoded #, got {fallback_url!r}"
    assert "#" not in fallback_url.split("?", 1)[0], (
        f"Raw # leaked into path: {fallback_url!r}"
    )


def test_bulk_get_empty_input_short_circuits(unit_client):
    """No HTTP fired when called with an empty doc_revs list."""
    assert unit_client.sync.bulk_get([]) == []


# --------------------------- _bulk_docs ------------------------------------


def test_bulk_docs_sends_new_edits_false(mock_http, unit_client):
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_docs",
        json=[
            {"id": "doc-a", "rev": "5-newrev1"},
            {"id": "doc-b", "rev": "4-newrev2"},
        ],
        status=201,
    )

    out = unit_client.sync.bulk_docs([
        {"_id": "doc-a", "_rev": "5-newrev1", "data": "x"},
        {"_id": "doc-b", "_rev": "4-newrev2", "data": "y"},
    ])

    assert [e["id"] for e in out] == ["doc-a", "doc-b"]
    req = mock_http.calls[0].request
    _assert_cookie_only_auth(req)
    body = json.loads(req.body)
    assert body["new_edits"] is False
    assert [d["_id"] for d in body["docs"]] == ["doc-a", "doc-b"]


def test_bulk_docs_new_edits_true_round_trips(mock_http, unit_client):
    mock_http.post(f"{TEST_NEOCLOUD_BASE}/_bulk_docs", json=[], status=201)
    unit_client.sync.bulk_docs([{"_id": "x"}], new_edits=True)
    body = json.loads(mock_http.calls[0].request.body)
    assert body["new_edits"] is True


# --------------------------- _revs_diff ------------------------------------


def test_revs_diff_happy_path(mock_http, unit_client):
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_revs_diff",
        json={
            "doc-a": {
                "missing": ["5-aaa"],
                "possible_ancestors": ["4-bbb"],
            },
        },
    )

    out = unit_client.sync.revs_diff({"doc-a": ["5-aaa"], "doc-b": ["1-old"]})
    assert out == {"doc-a": {"missing": ["5-aaa"], "possible_ancestors": ["4-bbb"]}}
    req = mock_http.calls[0].request
    _assert_cookie_only_auth(req)
    assert json.loads(req.body) == {"doc-a": ["5-aaa"], "doc-b": ["1-old"]}


def test_revs_diff_non_dict_response_raises(mock_http, unit_client):
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_revs_diff",
        json=["not", "a", "dict"],
    )
    with pytest.raises(SyncProtocolError):
        unit_client.sync.revs_diff({"doc-a": ["1-x"]})


# --------------------------- _local ----------------------------------------


def test_local_get_happy_path(mock_http, unit_client):
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_local/checkpoint-key",
        json={
            "_id": "_local/checkpoint-key",
            "_rev": "0-5",
            "last_seq": "1::100",
            "session_id": "sess-abc",
        },
    )
    doc = unit_client.sync.local_get("checkpoint-key")
    assert doc["last_seq"] == "1::100"
    _assert_cookie_only_auth(mock_http.calls[0].request)


def test_local_get_404_returns_none(mock_http, unit_client):
    """A fresh checkpoint (404 not_found) returns ``None``, not an exception."""
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_local/missing-key",
        json={"error": "not_found", "reason": "missing"},
        status=404,
    )
    assert unit_client.sync.local_get("missing-key") is None


def test_local_put_round_trip(mock_http, unit_client):
    mock_http.put(
        f"{TEST_NEOCLOUD_BASE}/_local/checkpoint-key",
        json={"id": "_local/checkpoint-key", "ok": True, "rev": "0-6"},
        status=201,
    )
    doc = {
        "_id": "_local/checkpoint-key",
        "_rev": "0-5",
        "last_seq": "1::101",
        "session_id": "sess-xyz",
    }
    out = unit_client.sync.local_put("checkpoint-key", doc)
    assert out == {"id": "_local/checkpoint-key", "ok": True, "rev": "0-6"}
    req = mock_http.calls[0].request
    assert req.method == "PUT"
    _assert_cookie_only_auth(req)
    assert json.loads(req.body) == doc


def test_local_put_get_round_trip(mock_http, unit_client):
    """PUT then GET returns the stored doc unchanged at the wire level."""
    stored = {
        "_id": "_local/k",
        "_rev": "0-1",
        "last_seq": "1::42",
        "session_id": "s",
    }
    mock_http.put(
        f"{TEST_NEOCLOUD_BASE}/_local/k",
        json={"id": "_local/k", "ok": True, "rev": "0-2"},
        status=201,
    )
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_local/k",
        json=stored,
    )

    unit_client.sync.local_put("k", stored)
    fetched = unit_client.sync.local_get("k")
    assert fetched == stored


# --------------------------- auth boundary --------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.sync.changes("ch"),
        lambda c: c.sync.bulk_get([{"id": "a", "rev": "1-x"}]),
        lambda c: c.sync.bulk_docs([{"_id": "a"}]),
        lambda c: c.sync.revs_diff({"a": ["1-x"]}),
        lambda c: c.sync.local_get("k"),
        lambda c: c.sync.local_put("k", {"_id": "_local/k"}),
    ],
    ids=["changes", "bulk_get", "bulk_docs", "revs_diff", "local_get", "local_put"],
)
def test_cookie_missing_raises_authError(unit_client, call):
    """Every primitive refuses to fire if ``client.sync_token`` is None."""
    unit_client.sync_token = None
    with pytest.raises(AuthError) as exc_info:
        call(unit_client)
    # Message must point the caller at how to fix it.
    assert "mint_sync_session" in str(exc_info.value)


def test_no_bearer_header_on_neocloud(mock_http, unit_client):
    """Sanity: even with a Bearer token set, /neocloud/* never carries it."""
    unit_client.token = "should-not-be-sent"  # pragma: allowlist secret
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_changes",
        json={"results": [], "last_seq": "0"},
    )
    unit_client.sync.changes("ch")
    req = mock_http.calls[0].request
    assert "Authorization" not in req.headers
    assert f"SyncGatewaySession={TEST_SYNC_TOKEN}" in req.headers.get("Cookie", "")


def test_401_raises_authError(mock_http, unit_client):
    """Expired cookie → server 401 → ``AuthError``."""
    mock_http.get(
        f"{TEST_NEOCLOUD_BASE}/_changes",
        json={"error": "Unauthorized"},
        status=401,
    )
    with pytest.raises(AuthError):
        unit_client.sync.changes("ch")
