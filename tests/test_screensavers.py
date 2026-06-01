"""Unit tests for ``boox.screensavers``.

Covers the Pattern A ``ScreensaversClient`` subobject wired onto
``BooxClient`` (#33). Mirrors :mod:`tests.test_files` shape — the
screensaver push flow shares OSS upload + Sync Gateway scaffolding
with BooxDrop files but routes through the dedicated
``screenSavers/push`` endpoint with a ``cbMsg`` callback ID.

Endpoint coverage:

- ``GET  /api/1/config/stss``                — HAR-confirmed.
- ``POST /neocloud/_bulk_docs``              — HAR-confirmed
  (``contentType: "push_screensaver"``).
- ``POST /api/1/screenSavers/push``          — HAR-confirmed (entry 104).
- ``GET  /api/1/push/message``               — HAR-inferred for
  ``sourceType=100`` (the listing surface returns screensaver entries
  with that filter; see :mod:`boox.screensavers` module docstring).
- ``POST /api/1/push/message/batchDelete``   — HAR-inferred for the
  screensaver category (same delete endpoint as BooxDrop files).
"""

import json
from urllib.parse import urlparse, parse_qs

import pytest

import boox
from boox.screensavers import ScreensaversClient
from .conftest import (
    TEST_API_BASE,
    TEST_NEOCLOUD_BASE,
    TEST_SYNC_TOKEN,
    TEST_TOKEN,
)


# --------------------------- Pattern A wiring ------------------------------


def test_screensavers_subobject_wired_on_client(boox_config):
    """``BooxClient.__init__`` wires the ScreensaversClient subobject."""
    client = boox.Boox(boox_config, skip_init=True)
    assert isinstance(client.screensavers, ScreensaversClient)
    assert client.screensavers._c is client


# --------------------------- Shared fixtures -------------------------------


_STSS_DATA = {
    "AccessKeyId": "STS.FixtureKey",  # pragma: allowlist secret
    "AccessKeySecret": "FixtureSecret",  # pragma: allowlist secret
    "SecurityToken": "FixtureToken",  # pragma: allowlist secret
}


@pytest.fixture
def push_ready_client(unit_client):
    """Client primed with cloud-side config that ``push_screensaver`` reads."""
    unit_client.userid = "user-uid-fixture"
    unit_client.bucket_name = "onyx-cloud-test"
    unit_client.endpoint = "oss-test.aliyuncs.com"
    return unit_client


@pytest.fixture
def oss_mocks(mocker):
    """Mock the oss2 layer so ``push_screensaver`` doesn't actually upload."""
    bucket_instance = mocker.MagicMock()
    mocker.patch.object(boox.oss2, "Bucket", return_value=bucket_instance)
    mocker.patch.object(boox.oss2, "Auth")
    resumable = mocker.patch.object(boox.oss2, "resumable_upload")
    return {"bucket": bucket_instance, "resumable_upload": resumable}


def _stub_push_endpoints(mock_http):
    """Register canned responses for the three HTTP calls push_screensaver makes."""
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
        f"{TEST_API_BASE}/screenSavers/push",
        json={"result_code": 0, "data": {"_id": "screensaver-id-xyz"}},
    )


def _screenSavers_payload(mock_http):
    """Extract the parsed body from the (single) screenSavers/push call."""
    call = next(
        c for c in mock_http.calls if "screenSavers/push" in c.request.url
    )
    return json.loads(call.request.body)


def _bulk_docs_body(mock_http):
    """Extract the (single) parsed bulk_docs request body."""
    bulk_call = next(
        c for c in mock_http.calls if "neocloud/_bulk_docs" in c.request.url
    )
    return json.loads(bulk_call.request.body)


# --------------------------- push_screensaver: orchestration ---------------


def test_push_screensaver_fetches_sts_credentials(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """push_screensaver pre-fetches STS credentials before uploading."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    stss_calls = [c for c in mock_http.calls if "config/stss" in c.request.url]
    assert len(stss_calls) == 1


def test_push_screensaver_invokes_oss_upload(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """push_screensaver invokes the OSS upload path for the image."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    assert oss_mocks["resumable_upload"].called


def test_push_screensaver_bulk_docs_before_screenSavers_push(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """bulk_docs strictly precedes screenSavers/push — the cbMsg id+rev
    refer to the bulk_docs registration."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    urls = [c.request.url for c in mock_http.calls]
    bulk_idx = next(i for i, u in enumerate(urls) if "neocloud/_bulk_docs" in u)
    push_idx = next(i for i, u in enumerate(urls) if "screenSavers/push" in u)
    assert bulk_idx < push_idx


def test_push_screensaver_returns_response_envelope(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """push_screensaver returns the parsed screenSavers/push response."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    result = push_ready_client.screensavers.push_screensaver(str(f))

    assert result == {"result_code": 0, "data": {"_id": "screensaver-id-xyz"}}


# --------------------------- push_screensaver: screenSavers/push body ------


def test_push_screensaver_endpoint_is_screenSavers_push(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """The dedicated ``/api/1/screenSavers/push`` endpoint is hit."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    push_calls = [
        c for c in mock_http.calls if "screenSavers/push" in c.request.url
    ]
    assert len(push_calls) == 1
    assert push_calls[0].request.method == "POST"


def test_push_screensaver_body_carries_data_and_cbMsg(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """Body shape: ``{"data": {...}, "cbMsg": {"id", "rev"}}`` (HAR entry 104)."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    body = _screenSavers_payload(mock_http)
    assert set(body.keys()) == {"data", "cbMsg"}
    assert set(body["cbMsg"].keys()) == {"id", "rev"}


def test_push_screensaver_cbMsg_matches_bulk_docs_id_and_rev(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """cbMsg.id and cbMsg.rev mirror the bulk_docs ``_id`` / ``_rev``
    so Boox can join the two server-side records."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    bulk_doc = _bulk_docs_body(mock_http)["docs"][0]
    push_body = _screenSavers_payload(mock_http)
    assert push_body["cbMsg"]["id"] == bulk_doc["_id"]
    assert push_body["cbMsg"]["rev"] == bulk_doc["_rev"]


@pytest.mark.parametrize(
    "filename,expected_type",
    [
        ("wallpaper.png", "png"),
        ("dragon.jpg", "jpg"),
        ("photo.jpeg", "jpeg"),
        ("art.webp", "webp"),
    ],
)
def test_push_screensaver_derives_resource_type_from_extension(
    mock_http, push_ready_client, oss_mocks, tmp_path, filename, expected_type
):
    """resourceType derives from extension (same as files module)."""
    f = tmp_path / filename
    f.write_bytes(b"placeholder")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    body = _screenSavers_payload(mock_http)
    assert body["data"]["resourceType"] == expected_type


def test_push_screensaver_data_carries_basename_bucket_and_key(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``data`` envelope carries basename, per-user bucket, OSS key."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    data = _screenSavers_payload(mock_http)["data"]
    assert data["name"] == "wallpaper.png"
    assert data["resourceDisplayName"] == "wallpaper.png"
    assert data["bucket"] == "onyx-cloud-test"
    assert data["resourceKey"].startswith("user-uid-fixture/push/")
    assert data["resourceKey"].endswith(".png")
    assert ".." not in data["resourceKey"]


def test_push_screensaver_default_title_is_basename(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``title=None`` → uses basename in both data envelope and bulk_docs."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    data = _screenSavers_payload(mock_http)["data"]
    assert data["title"] == "wallpaper.png"
    bulk_doc = _bulk_docs_body(mock_http)["docs"][0]
    inner = json.loads(bulk_doc["content"])
    assert inner["title"] == "wallpaper.png"


def test_push_screensaver_explicit_title_overrides_basename(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``title="..."`` overrides on both screenSavers/push + bulk_docs sides."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f), title="My Wallpaper")

    data = _screenSavers_payload(mock_http)["data"]
    assert data["title"] == "My Wallpaper"
    # name + resourceDisplayName remain the on-disk basename.
    assert data["name"] == "wallpaper.png"
    assert data["resourceDisplayName"] == "wallpaper.png"
    bulk_doc = _bulk_docs_body(mock_http)["docs"][0]
    inner = json.loads(bulk_doc["content"])
    assert inner["title"] == "My Wallpaper"


def test_push_screensaver_parent_is_always_null(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``data.parent`` is always ``null`` — HAR shows no folder support
    on the screensaver surface."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    data = _screenSavers_payload(mock_http)["data"]
    assert data["parent"] is None


def test_push_screensaver_sends_bearer_on_screenSavers_push(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """screenSavers/push carries the Bearer JWT."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    call = next(
        c for c in mock_http.calls if "screenSavers/push" in c.request.url
    )
    assert call.request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"


# --------------------------- push_screensaver: bulk_docs -------------------


def test_push_screensaver_bulk_docs_uses_push_screensaver_content_type(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """bulk_docs ``contentType`` is ``"push_screensaver"`` (HAR entry 101)."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    doc = _bulk_docs_body(mock_http)["docs"][0]
    assert doc["contentType"] == "push_screensaver"


def test_push_screensaver_bulk_docs_uses_message_channel_and_session_cookie(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """bulk_docs body uses ``<user_uid>-MESSAGE`` dbId + Sync Gateway cookie."""
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    push_ready_client.screensavers.push_screensaver(str(f))

    bulk_call = next(
        c for c in mock_http.calls if "neocloud/_bulk_docs" in c.request.url
    )
    doc = json.loads(bulk_call.request.body)["docs"][0]
    assert doc["dbId"] == "user-uid-fixture-MESSAGE"
    # Auth: SyncGatewaySession cookie, no Bearer header.
    auth = bulk_call.request.headers.get("Authorization", "")
    assert "Bearer" not in auth
    cookie = bulk_call.request.headers.get("Cookie", "")
    assert f"SyncGatewaySession={TEST_SYNC_TOKEN}" in cookie


def test_push_screensaver_skips_bulk_docs_when_sync_token_missing(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """Without ``sync_token`` we log + skip bulk_docs but still hit
    screenSavers/push (same fallback shape as :meth:`FilesClient.push_file`)."""
    push_ready_client.sync_token = None
    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    mock_http.get(
        f"{TEST_API_BASE}/config/stss",
        json={"result_code": 0, "data": _STSS_DATA},
    )
    mock_http.post(
        f"{TEST_API_BASE}/screenSavers/push",
        json={"result_code": 0, "data": {"_id": "ss-id"}},
    )

    push_ready_client.screensavers.push_screensaver(str(f))

    urls = [c.request.url for c in mock_http.calls]
    assert not any("neocloud/_bulk_docs" in u for u in urls)
    assert any("screenSavers/push" in u for u in urls)


# --------------------------- push_screensaver: OSS error wrapping ---------


def test_push_screensaver_wraps_oss_failures_in_oss_error(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """oss2 failures wrap in :class:`boox.errors.OSSError` (#28)."""
    import oss2

    from boox.errors import OSSError

    f = tmp_path / "wallpaper.png"
    f.write_bytes(b"\x89PNG\r\n")
    _stub_push_endpoints(mock_http)

    oss_exc = oss2.exceptions.OssError(500, {}, "<Error/>", {})
    oss_mocks["resumable_upload"].side_effect = oss_exc

    with pytest.raises(OSSError) as excinfo:
        push_ready_client.screensavers.push_screensaver(str(f))

    assert excinfo.value.__cause__ is oss_exc


# --------------------------- list_screensavers -----------------------------


_SAMPLE_SCREENSAVER_ENTRY = {
    "data": {
        "args": {
            "_id": "ss-fixture-id",
            "name": "wallpaper.png",
            "formats": ["png"],
            "sourceType": 100,
            "storage": {
                "png": {
                    "oss": {
                        "size": "1024",
                        "key": "user-uid-fixture/push/uuid.png",
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


def test_list_screensavers_request_shape(mock_http, unit_client):
    """Hits push/message with Bearer + JSON ``where`` filter that pins
    ``sourceType=100``."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_SCREENSAVER_ENTRY]},
    )

    unit_client.screensavers.list_screensavers(limit=10, offset=5)

    call = mock_http.calls[0]
    assert call.request.url.startswith(f"{TEST_API_BASE}/push/message")
    assert call.request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    where = _captured_where(call)
    assert where == {"limit": 10, "offset": 5, "sourceType": 100}


def test_list_screensavers_defaults_pin_source_type_100(mock_http, unit_client):
    """Default args pin ``sourceType=100`` — the screensaver-category filter."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.screensavers.list_screensavers()

    where = _captured_where(mock_http.calls[0])
    assert where == {"limit": 30, "offset": 0, "sourceType": 100}


def test_list_screensavers_returns_parsed_list(mock_http, unit_client):
    """Returns the raw ``list`` envelope; no stdout side effect."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_SCREENSAVER_ENTRY]},
    )

    result = unit_client.screensavers.list_screensavers()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["data"]["args"]["_id"] == "ss-fixture-id"


# --------------------------- delete_screensavers ---------------------------


def test_delete_screensavers_routes_to_batchDelete(mock_http, unit_client):
    """Hits push/message/batchDelete with the ``{"ids": [...]}`` body."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    result = unit_client.screensavers.delete_screensavers(["ss-id-1"])

    assert result == {"result_code": 0, "data": "ok"}
    req = mock_http.calls[0].request
    assert req.method == "POST"
    assert req.url.endswith("/push/message/batchDelete")
    assert req.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(req.body) == {"ids": ["ss-id-1"]}


def test_delete_screensavers_batch(mock_http, unit_client):
    """Multi-ID delete in a single call."""
    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 0, "data": "ok"},
    )

    unit_client.screensavers.delete_screensavers(["a", "b", "c"])

    assert json.loads(mock_http.calls[0].request.body) == {"ids": ["a", "b", "c"]}


def test_delete_screensavers_empty_list_short_circuits(mock_http, unit_client):
    """Empty list returns ``None`` without hitting the server (matches
    :meth:`FilesClient.delete_files` convention)."""
    result = unit_client.screensavers.delete_screensavers([])

    assert result is None
    assert len(mock_http.calls) == 0


def test_delete_screensavers_raises_apierror_on_nonzero_result_code(
    mock_http, unit_client
):
    """Non-zero ``result_code`` surfaces as :class:`boox.errors.APIError`."""
    from boox.errors import APIError

    mock_http.post(
        f"{TEST_API_BASE}/push/message/batchDelete",
        json={"result_code": 1, "message": "boom", "data": None},
    )

    with pytest.raises(APIError) as excinfo:
        unit_client.screensavers.delete_screensavers(["x"])
    assert excinfo.value.result_code == 1
