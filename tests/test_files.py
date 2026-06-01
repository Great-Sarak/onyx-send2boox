"""Unit tests for ``boox.files``.

Covers the Pattern A ``FilesClient`` subobject wired onto ``BooxClient``
(#32). Mirrors the test patterns from Phase 0's ``test_file_push.py`` /
``test_file_listing.py`` / ``test_delete.py`` so the new module's
end-to-end behaviors are asserted in the same shape — the legacy
flat-client tests stay as-is and continue to guard hrw's top-level
scripts.

Endpoint coverage (all HAR-confirmed except cloudFiles/download/one,
which is bundle-referenced; see :mod:`boox.files`):

- ``GET  /api/1/config/stss``              — HAR-confirmed.
- ``POST /neocloud/_bulk_docs``            — HAR-confirmed.
- ``POST /api/1/push/saveAndPush``         — HAR-confirmed.
- ``GET  /api/1/push/message``             — HAR-confirmed.
- ``POST /api/1/push/message/batchDelete`` — HAR-confirmed.
- ``GET  /api/1/cloudFiles/download/one``  — bundle-referenced only.
"""

import json
import time
from urllib.parse import urlparse, parse_qs

import pytest

import boox
from boox.files import FilesClient
from .conftest import (
    TEST_API_BASE,
    TEST_NEOCLOUD_BASE,
    TEST_SYNC_TOKEN,
    TEST_TOKEN,
)


# --------------------------- Pattern A wiring ------------------------------


def test_files_subobject_wired_on_client(boox_config):
    """``BooxClient.__init__`` wires the FilesClient subobject."""
    client = boox.Boox(boox_config, skip_init=True)
    assert isinstance(client.files, FilesClient)
    assert client.files._c is client


# --------------------------- Shared fixtures -------------------------------


_STSS_DATA = {
    "AccessKeyId": "STS.FixtureKey",  # pragma: allowlist secret
    "AccessKeySecret": "FixtureSecret",  # pragma: allowlist secret
    "SecurityToken": "FixtureToken",  # pragma: allowlist secret
}


@pytest.fixture
def push_ready_client(unit_client):
    """Client primed with cloud-side config that ``push_file`` reads.

    Mirrors the Phase 0 ``test_file_push.py`` fixture; the per-user OSS
    bucket / endpoint / userid are normally set by ``__init__``'s init
    chain, but unit tests skip that so we set them directly.
    """
    unit_client.userid = "user-uid-fixture"
    unit_client.bucket_name = "onyx-cloud-test"
    unit_client.endpoint = "oss-test.aliyuncs.com"
    return unit_client


@pytest.fixture
def oss_mocks(mocker):
    """Mock the oss2 layer so ``push_file`` doesn't actually upload."""
    bucket_instance = mocker.MagicMock()
    mocker.patch.object(boox.oss2, "Bucket", return_value=bucket_instance)
    mocker.patch.object(boox.oss2, "Auth")
    resumable = mocker.patch.object(boox.oss2, "resumable_upload")
    return {"bucket": bucket_instance, "resumable_upload": resumable}


def _stub_push_endpoints(mock_http):
    """Register canned responses for the three HTTP calls ``push_file`` makes."""
    mock_http.get(
        f"{TEST_API_BASE}/config/stss",
        json={"result_code": 0, "data": _STSS_DATA},
    )
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_docs",
        json=[{"id": "doc-id", "rev": "1-fixturerev"}],
        status=201,
    )
    mock_http.post(
        f"{TEST_API_BASE}/push/saveAndPush",
        json={"result_code": 0, "data": "ok"},
    )


def _saveandpush_payload(mock_http):
    """Extract the ``data`` payload from the (single) saveAndPush call."""
    save_call = next(
        c for c in mock_http.calls if "saveAndPush" in c.request.url
    )
    body = json.loads(save_call.request.body)
    return body["data"]


def _bulk_docs_body(mock_http):
    """Extract the (single) parsed bulk_docs request body."""
    bulk_call = next(
        c for c in mock_http.calls if "neocloud/_bulk_docs" in c.request.url
    )
    return json.loads(bulk_call.request.body)


# --------------------------- push_file: orchestration ----------------------


def test_push_file_fetches_sts_credentials(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """push_file pre-fetches STS credentials before uploading."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    stss_calls = [c for c in mock_http.calls if "config/stss" in c.request.url]
    assert len(stss_calls) == 1, "push_file should fetch STS credentials once"


def test_push_file_invokes_oss_upload(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """push_file invokes the OSS upload path for the file."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    assert oss_mocks["resumable_upload"].called


def test_push_file_bulk_docs_before_saveAndPush(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """A complete push hits /neocloud/_bulk_docs strictly before saveAndPush.

    Mirrors the Phase 0 ``test_send_file_posts_bulk_docs_before_saveAndPush``
    assertion — the order is load-bearing for the NaN-timestamp fix.
    """
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    urls = [c.request.url for c in mock_http.calls]
    bulk_idx = next(i for i, u in enumerate(urls) if "neocloud/_bulk_docs" in u)
    save_idx = next(i for i, u in enumerate(urls) if "saveAndPush" in u)
    assert bulk_idx < save_idx


def test_push_file_returns_saveAndPush_response(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """push_file returns the parsed saveAndPush response envelope."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    result = push_ready_client.files.push_file(str(f))

    assert result == {"result_code": 0, "data": "ok"}


# --------------------------- push_file: resourceType -----------------------


@pytest.mark.parametrize(
    "filename,expected_type",
    [
        ("book.pdf", "pdf"),
        ("story.epub", "epub"),
        ("comic.cbz", "cbz"),
        ("doc.txt", "txt"),
        ("ebook.mobi", "mobi"),
    ],
)
def test_push_file_derives_resource_type_from_extension(
    mock_http, push_ready_client, oss_mocks, tmp_path, filename, expected_type
):
    """resourceType derives from the extension (Phase 0 #5 carryover)."""
    f = tmp_path / filename
    f.write_bytes(b"placeholder")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["resourceType"] == expected_type


def test_push_file_dotless_filename_falls_back_to_bin(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """A dotless filename pushes with ``resourceType="bin"``."""
    f = tmp_path / "README"
    f.write_bytes(b"placeholder")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["resourceType"] == "bin"


# --------------------------- push_file: saveAndPush metadata ---------------


def test_push_file_saveAndPush_carries_basename_and_bucket(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """saveAndPush body uses the file's basename + the per-user bucket."""
    f = tmp_path / "story.epub"
    f.write_bytes(b"content")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["name"] == "story.epub"
    assert payload["resourceDisplayName"] == "story.epub"
    assert payload["bucket"] == "onyx-cloud-test"
    assert payload["resourceKey"].startswith("user-uid-fixture/push/")


def test_push_file_default_title_is_basename(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``title=None`` → saveAndPush uses the basename."""
    f = tmp_path / "story.epub"
    f.write_bytes(b"content")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["title"] == "story.epub"


def test_push_file_explicit_title_overrides_basename(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``title="..."`` overrides basename on saveAndPush + bulk_docs."""
    f = tmp_path / "story.epub"
    f.write_bytes(b"content")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f), title="My Story")

    payload = _saveandpush_payload(mock_http)
    assert payload["title"] == "My Story"
    # name + resourceDisplayName stay as the on-disk basename.
    assert payload["name"] == "story.epub"
    assert payload["resourceDisplayName"] == "story.epub"
    # bulk_docs content carries the same title (web UI flow includes it).
    bulk_doc = _bulk_docs_body(mock_http)["docs"][0]
    inner = json.loads(bulk_doc["content"])
    assert inner["title"] == "My Story"


def test_push_file_default_parent_is_none(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``parent=None`` (default) means top-level inbox."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["parent"] is None


def test_push_file_explicit_parent_is_forwarded(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``parent="folder-id"`` forwards verbatim to saveAndPush."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f), parent="folder-id-fixture")

    payload = _saveandpush_payload(mock_http)
    assert payload["parent"] == "folder-id-fixture"


def test_push_file_oss_key_has_single_dot_extension(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """OSS key uses ``<uuid>.<ext>``, not ``<uuid>..<ext>`` (Phase 0 #7)."""
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    payload = _saveandpush_payload(mock_http)
    # Per-user prefix + push/ + uuid + .pdf — exactly one dot before ext.
    key = payload["resourceKey"]
    assert key.startswith("user-uid-fixture/push/")
    assert key.endswith(".pdf")
    assert ".." not in key, f"OSS key has double-dot: {key!r}"


def test_push_file_sends_bearer_on_saveAndPush(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """saveAndPush carries the Bearer JWT (different api_call code path
    than bare GETs because the body is non-empty)."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    save_call = next(
        c for c in mock_http.calls if "saveAndPush" in c.request.url
    )
    assert save_call.request.headers["Authorization"].startswith("Bearer ")


# --------------------------- push_file: bulk_docs --------------------------


def test_push_file_bulk_docs_uses_message_channel_and_session_cookie(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """bulk_docs body uses ``<user_uid>-MESSAGE`` dbId + Sync Gateway cookie."""
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    bulk_call = next(
        c for c in mock_http.calls if "neocloud/_bulk_docs" in c.request.url
    )
    doc = json.loads(bulk_call.request.body)["docs"][0]
    assert doc["dbId"] == "user-uid-fixture-MESSAGE"
    assert doc["contentType"] == "digital_content"
    # Auth: SyncGatewaySession cookie, no Bearer header.
    auth = bulk_call.request.headers.get("Authorization", "")
    assert "Bearer" not in auth
    cookie = bulk_call.request.headers.get("Cookie", "")
    assert f"SyncGatewaySession={TEST_SYNC_TOKEN}" in cookie


def test_push_file_bulk_docs_carries_epoch_ms_timestamps(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """createdAt + updatedAt land as epoch-ms ints (the NaN fix)."""
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    push_ready_client.files.push_file(str(f))

    doc = _bulk_docs_body(mock_http)["docs"][0]
    assert isinstance(doc["createdAt"], int)
    assert isinstance(doc["updatedAt"], int)
    now_ms = time.time() * 1000
    assert abs(doc["updatedAt"] - now_ms) < 5 * 60 * 1000


def test_push_file_skips_bulk_docs_when_sync_token_missing(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """Without ``sync_token`` we log + skip bulk_docs but still proceed."""
    push_ready_client.sync_token = None
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    mock_http.get(
        f"{TEST_API_BASE}/config/stss",
        json={"result_code": 0, "data": _STSS_DATA},
    )
    mock_http.post(
        f"{TEST_API_BASE}/push/saveAndPush",
        json={"result_code": 0, "data": "ok"},
    )
    # Note: no /neocloud/_bulk_docs response registered; if push_file
    # attempts to call it the responses fixture raises.

    push_ready_client.files.push_file(str(f))

    urls = [c.request.url for c in mock_http.calls]
    assert not any("neocloud/_bulk_docs" in u for u in urls)
    # saveAndPush still fires.
    assert any("saveAndPush" in u for u in urls)


# --------------------------- push_file: OSS error wrapping -----------------


def test_push_file_wraps_oss_failures_in_oss_error(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """oss2 failures wrap in :class:`boox.errors.OSSError` (#28)."""
    import oss2

    from boox.errors import OSSError

    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_push_endpoints(mock_http)

    oss_exc = oss2.exceptions.OssError(500, {}, "<Error/>", {})
    oss_mocks["resumable_upload"].side_effect = oss_exc

    with pytest.raises(OSSError) as excinfo:
        push_ready_client.files.push_file(str(f))

    assert excinfo.value.__cause__ is oss_exc
    assert excinfo.value.oss_exception is oss_exc


# --------------------------- list_files ------------------------------------


_SAMPLE_LISTING_ENTRY = {
    "data": {
        "args": {
            "_id": "abc123fixtureid",
            "name": "book.pdf",
            "formats": ["pdf"],
            "storage": {
                "pdf": {
                    "oss": {
                        "size": "1234567",
                        "key": "user-uid-fixture/push/uuid.pdf",
                        "bucket": "onyx-cloud-test",
                        "url": "https://oss-fixture/signed-url",
                    }
                }
            },
            "createdAt": 1780270000000,
            "updatedAt": 1780270000000,
        }
    }
}


def _captured_where(call):
    qs = parse_qs(urlparse(call.request.url).query)
    return json.loads(qs["where"][0])


def test_list_files_request_shape(mock_http, unit_client):
    """Hits push/message with Bearer + JSON where filter."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_LISTING_ENTRY]},
    )

    unit_client.files.list_files(limit=10, offset=5)

    call = mock_http.calls[0]
    assert call.request.url.startswith(f"{TEST_API_BASE}/push/message")
    assert call.request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    where = _captured_where(call)
    assert where == {"limit": 10, "offset": 5, "parent": 0}


def test_list_files_defaults(mock_http, unit_client):
    """Default args produce (limit=30, offset=0, parent=0) per issue body."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.files.list_files()

    where = _captured_where(mock_http.calls[0])
    assert where == {"limit": 30, "offset": 0, "parent": 0}


def test_list_files_screensaver_source_type(mock_http, unit_client):
    """``source_type=100`` filters to screensavers."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.files.list_files(source_type=100)

    where = _captured_where(mock_http.calls[0])
    assert where["sourceType"] == 100


def test_list_files_custom_parent(mock_http, unit_client):
    """``parent="folder-id"`` filters to that folder's contents."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.files.list_files(parent="folder-id-fixture")

    where = _captured_where(mock_http.calls[0])
    assert where["parent"] == "folder-id-fixture"


def test_list_files_returns_parsed_list(mock_http, unit_client):
    """Returns the raw ``list`` envelope; does NOT print a table."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_LISTING_ENTRY]},
    )

    result = unit_client.files.list_files()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["data"]["args"]["_id"] == "abc123fixtureid"


def test_list_files_no_stdout_side_effect(mock_http, unit_client, capsys):
    """The new Pattern A list_files is silent (legacy method prints)."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_LISTING_ENTRY]},
    )

    unit_client.files.list_files()

    captured = capsys.readouterr()
    assert captured.out == ""


# --------------------------- download_file ---------------------------------
#
# cloudFiles/download/one is bundle-referenced only — see boox.files
# module docstring. The wrapper accepts either response shape (bare
# signed-URL string or {"url": ...} object); tests assert both work.


_FIXTURE_SIGNED_URL = "https://oss-fixture/cf-signed-url-xyz"


def test_download_file_two_step_flow_with_string_data(
    mock_http, unit_client
):
    """Boox returns ``data`` as a bare signed-URL string."""
    mock_http.get(
        f"{TEST_API_BASE}/cloudFiles/download/one",
        json={"result_code": 0, "data": _FIXTURE_SIGNED_URL},
    )
    mock_http.get(_FIXTURE_SIGNED_URL, body=b"\x89PNG...payload-bytes")

    result = unit_client.files.download_file("cloud-file-id-xyz")

    assert result == b"\x89PNG...payload-bytes"
    # Two calls in order: API envelope, then OSS fetch.
    assert len(mock_http.calls) == 2
    api_call = mock_http.calls[0]
    qs = parse_qs(urlparse(api_call.request.url).query)
    assert qs["id"] == ["cloud-file-id-xyz"]
    assert api_call.request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    # OSS fetch must not re-send the Bearer (the signed URL self-authorizes).
    oss_call = mock_http.calls[1]
    assert "Authorization" not in oss_call.request.headers or (
        "Bearer" not in oss_call.request.headers.get("Authorization", "")
    )


def test_download_file_accepts_object_data_with_url_field(
    mock_http, unit_client
):
    """Boox returns ``data`` as ``{"url": "..."}`` — also accepted."""
    mock_http.get(
        f"{TEST_API_BASE}/cloudFiles/download/one",
        json={
            "result_code": 0,
            "data": {"url": _FIXTURE_SIGNED_URL, "size": 7},
        },
    )
    mock_http.get(_FIXTURE_SIGNED_URL, body=b"payload")

    result = unit_client.files.download_file("cloud-file-id-xyz")

    assert result == b"payload"


def test_download_file_writes_bytes_to_out_path(
    mock_http, unit_client, tmp_path
):
    """``out_path`` writes the bytes to disk in addition to returning them."""
    mock_http.get(
        f"{TEST_API_BASE}/cloudFiles/download/one",
        json={"result_code": 0, "data": _FIXTURE_SIGNED_URL},
    )
    mock_http.get(_FIXTURE_SIGNED_URL, body=b"on-disk-bytes")

    dest = tmp_path / "downloaded.pdf"
    result = unit_client.files.download_file("cf-id", out_path=str(dest))

    assert result == b"on-disk-bytes"
    assert dest.read_bytes() == b"on-disk-bytes"


def test_download_file_raises_when_no_url_in_response(mock_http, unit_client):
    """Empty / missing URL surfaces as ``ValueError`` — we don't fetch nothing."""
    mock_http.get(
        f"{TEST_API_BASE}/cloudFiles/download/one",
        json={"result_code": 0, "data": None},
    )

    with pytest.raises(ValueError):
        unit_client.files.download_file("cf-id")


def test_download_file_propagates_oss_http_errors(mock_http, unit_client):
    """OSS-side HTTP errors propagate (not wrapped as APIError)."""
    import requests

    mock_http.get(
        f"{TEST_API_BASE}/cloudFiles/download/one",
        json={"result_code": 0, "data": _FIXTURE_SIGNED_URL},
    )
    mock_http.get(_FIXTURE_SIGNED_URL, status=403, body="<Error/>")

    with pytest.raises(requests.HTTPError):
        unit_client.files.download_file("cf-id")


# --------------------------- delete_files ---------------------------------


def test_delete_files_routes_to_batchDelete(mock_http, unit_client):
    """Hits push/message/batchDelete with the ``{"ids": [...]}`` body."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    result = unit_client.files.delete_files(["file-id-1"])

    assert result == {"result_code": 0, "data": "ok"}
    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/push/message/batchDelete")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"ids": ["file-id-1"]}


def test_delete_files_batch(mock_http, unit_client):
    """Multi-ID delete in a single call."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.files.delete_files(["a", "b", "c"])

    assert json.loads(mock_http.calls[0].request.body) == {"ids": ["a", "b", "c"]}


def test_delete_files_empty_list_short_circuits(mock_http, unit_client):
    """Empty list returns ``None`` without hitting the server.

    Matches the :class:`boox.subscriptions.SubscriptionsClient.unsubscribe_many`
    convention — saves a wasted round-trip and lets callers like
    ``delete_files([e for e in entries if cond])`` no-op cleanly when
    nothing matches their filter.
    """
    # No mock registered: a network call would raise.
    result = unit_client.files.delete_files([])

    assert result is None
    assert len(mock_http.calls) == 0


def test_delete_files_raises_apierror_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` surfaces as :class:`boox.errors.APIError`."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.files.delete_files(["x"])
    assert excinfo.value.result_code == 1


# --------------------------- Surface distinction ---------------------------


def test_list_files_and_download_file_hit_different_surfaces(
    mock_http, unit_client
):
    """list_files hits push/message; download_file hits cloudFiles —
    asserting both call URLs documents the surface distinction the
    module docstring calls out."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )
    mock_http.get(
        f"{TEST_API_BASE}/cloudFiles/download/one",
        json={"result_code": 0, "data": _FIXTURE_SIGNED_URL},
    )
    mock_http.get(_FIXTURE_SIGNED_URL, body=b"x")

    unit_client.files.list_files()
    unit_client.files.download_file("cf-id")

    urls = [c.request.url for c in mock_http.calls]
    assert any("/push/message" in u for u in urls)
    assert any("/cloudFiles/download/one" in u for u in urls)


# --------------------------- move_file: NOT exposed ------------------------


def test_move_file_method_is_not_exposed():
    """``move_file`` is deliberately not exposed (no HAR + no bundle ref).

    See :mod:`boox.files` module docstring for the rationale. Asserting
    the method's absence here guards against silent re-introduction.
    """
    assert not hasattr(FilesClient, "move_file"), (
        "FilesClient.move_file should not be exposed — no HAR-confirmed "
        "or bundle-referenced move endpoint. See module docstring."
    )
