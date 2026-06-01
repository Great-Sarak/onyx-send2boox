"""Unit test suite — file push to BooxDrop.

Covers the ``send_file`` end-to-end flow against the web-UI-confirmed
behavior (2026-05-31 BooxDrop upload HAR):

1. ``config/stss`` — fetch OSS STS credentials (Bearer JWT auth).
2. OSS upload via ``oss2.resumable_upload`` (multipart internally — the
   web UI also uses multipart; gian-didom's "STS can't multipart" claim
   isn't supported by the actual web-UI behavior, so no put_object/
   resumable distinction is asserted here).
3. ``POST /neocloud/_bulk_docs`` to ``<user_uid>-MESSAGE`` channel,
   carrying ``createdAt`` + ``updatedAt`` as epoch-ms ints derived from
   the source file's mtime. Auth via SyncGatewaySession cookie. This
   step fixes the NaN-timestamp bug; without it the reader filters the
   file out.
4. ``POST /api/1/push/saveAndPush`` — register with Boox cloud (Bearer
   JWT). ``resourceType`` derived from the file extension (fixes hrw's
   hardcoded ``"txt"``).

Added by #5 (Unit test suite — file push to BooxDrop). Two known bugs
fixed inline (resourceType derivation, bulk_docs registration with
proper timestamps). One open question carried forward: the OSS key
double-dot format is its own targeted edge case in #7.
"""

import json
import time

import pytest

import boox
from .conftest import TEST_API_BASE, TEST_NEOCLOUD_BASE, TEST_SYNC_TOKEN


_STSS_DATA = {
    "AccessKeyId": "STS.FixtureKey",  # pragma: allowlist secret
    "AccessKeySecret": "FixtureSecret",  # pragma: allowlist secret
    "SecurityToken": "FixtureToken",  # pragma: allowlist secret
}


# --------------------------- Fixtures --------------------------------------


@pytest.fixture
def push_ready_client(unit_client):
    """Client primed with the cloud-side config that ``send_file`` reads.

    These attributes are normally set by ``__init__`` from ``config/buckets``;
    we set them directly because Phase 0 unit tests don't run the init chain.
    """
    unit_client.userid = "user-uid-fixture"
    unit_client.bucket_name = "onyx-cloud-test"
    unit_client.endpoint = "oss-test.aliyuncs.com"
    return unit_client


@pytest.fixture
def oss_mocks(mocker):
    """Mock the oss2 layer so ``send_file`` doesn't actually upload."""
    bucket_instance = mocker.MagicMock()
    mocker.patch.object(boox.oss2, "Bucket", return_value=bucket_instance)
    mocker.patch.object(boox.oss2, "Auth")
    resumable = mocker.patch.object(boox.oss2, "resumable_upload")
    return {"bucket": bucket_instance, "resumable_upload": resumable}


def _stub_stss_and_save(mock_http):
    """Register the HTTP calls send_file always makes: STS, bulk_docs, save."""
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
    """Extract the data payload from the (single) saveAndPush call."""
    save_call = [c for c in mock_http.calls if "saveAndPush" in c.request.url][0]
    body = json.loads(save_call.request.body)
    return body["data"]


# --------------------------- resourceType derivation (bug fix) -------------


@pytest.mark.parametrize(
    "filename,expected_type",
    [
        ("book.pdf", "pdf"),
        ("story.epub", "epub"),
        ("comic.cbz", "cbz"),
        ("comic.cbr", "cbr"),
        ("doc.txt", "txt"),
        ("manual.azw3", "azw3"),
        ("ebook.mobi", "mobi"),
    ],
)
def test_send_file_derives_resource_type_from_extension(
    mock_http, push_ready_client, oss_mocks, tmp_path, filename, expected_type
):
    """Each push carries ``resourceType`` matching the file's extension.

    Catches hrw's hardcoded ``"resourceType": "txt"`` for all uploads.
    """
    f = tmp_path / filename
    f.write_bytes(b"placeholder content")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["resourceType"] == expected_type


# --------------------------- saveAndPush metadata --------------------------


def test_send_file_saveAndPush_carries_metadata(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``saveAndPush`` body has the file's basename + correct bucket."""
    f = tmp_path / "story.epub"
    f.write_bytes(b"content")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    payload = _saveandpush_payload(mock_http)
    assert payload["name"] == "story.epub"
    assert payload["title"] == "story.epub"
    assert payload["resourceDisplayName"] == "story.epub"
    assert payload["bucket"] == "onyx-cloud-test"
    assert payload["parent"] is None
    # resourceKey shape — UUID-based, scoped under the user; the strict
    # single-dot extension format is asserted by #7 (targeted edge case).
    assert payload["resourceKey"].startswith("user-uid-fixture/push/")


def test_send_file_sends_bearer_on_saveAndPush(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """saveAndPush carries the Bearer JWT (cross-check; hits a different
    api_call code path because data is non-empty)."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    save_call = [c for c in mock_http.calls if "saveAndPush" in c.request.url][0]
    assert save_call.request.headers["Authorization"].startswith("Bearer ")


# --------------------------- OSS upload path -------------------------------


def test_send_file_invokes_oss_upload(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """send_file invokes *some* OSS upload path for the file (sanity)."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    upload_used = (
        oss_mocks["resumable_upload"].called or oss_mocks["bucket"].put_object.called
    )
    assert upload_used, "send_file did not call any OSS upload method"


def test_send_file_fetches_sts_credentials(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """send_file pre-fetches STS credentials before uploading."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    stss_calls = [c for c in mock_http.calls if "config/stss" in c.request.url]
    assert len(stss_calls) == 1, "send_file should fetch STS credentials once"


# --------------------------- Timestamp / reader visibility -----------------
#
# The web-UI BooxDrop HAR (2026-05-31) confirmed: a complete push does a
# POST /neocloud/_bulk_docs to <user_uid>-MESSAGE channel BEFORE
# push/saveAndPush. Auth is via SyncGatewaySession cookie (NOT Bearer).
# The doc carries createdAt + updatedAt as epoch-ms ints; without this,
# push/saveAndPush registers the file but the reader filters it out as
# NaN-timestamped.
#
# Also from that HAR: the web UI uses multipart upload (init+part+complete)
# against the STS token without issue. gian-didom's README claim about
# "STS can't multipart" isn't supported by the actual web-UI behavior, so
# no put_object-vs-resumable test here.


def test_send_file_posts_bulk_docs_before_saveAndPush(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """A complete push hits /neocloud/_bulk_docs strictly before saveAndPush."""
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    urls = [c.request.url for c in mock_http.calls]
    bulk_idx = next(i for i, u in enumerate(urls) if "neocloud/_bulk_docs" in u)
    save_idx = next(i for i, u in enumerate(urls) if "saveAndPush" in u)
    assert bulk_idx < save_idx, f"Expected _bulk_docs before saveAndPush; got {urls}"


def test_send_file_bulk_docs_carries_valid_timestamps(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """createdAt + updatedAt land as epoch-ms ints — the NaN fix."""
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    bulk_calls = [c for c in mock_http.calls if "neocloud/_bulk_docs" in c.request.url]
    assert bulk_calls
    doc = json.loads(bulk_calls[0].request.body)["docs"][0]
    assert isinstance(doc["createdAt"], int)
    assert isinstance(doc["updatedAt"], int)
    # Using file mtime, so close to "now" for a just-written tmp file.
    now_ms = time.time() * 1000
    assert abs(doc["updatedAt"] - now_ms) < 5 * 60 * 1000


def test_send_file_bulk_docs_uses_message_channel_and_session_cookie(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """bulk_docs body uses ``<user_uid>-MESSAGE`` dbId + Sync Gateway cookie."""
    f = tmp_path / "story.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

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


# --------------------------- Error paths -----------------------------------


def test_send_file_wraps_oss_failures_in_oss_error(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """OSS upload failures wrap in ``OSSError`` so callers don't import oss2 (#28)."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)
    import oss2

    from boox.errors import OSSError

    oss_exc = oss2.exceptions.OssError(500, {}, "<Error/>", {})
    oss_mocks["resumable_upload"].side_effect = oss_exc

    with pytest.raises(OSSError) as excinfo:
        push_ready_client.send_file(str(f))

    # __cause__ chain preserves the original oss2 exception for inspection.
    assert excinfo.value.__cause__ is oss_exc
    assert excinfo.value.oss_exception is oss_exc
