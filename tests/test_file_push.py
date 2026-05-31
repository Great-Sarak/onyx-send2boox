"""Unit test suite — file push to BooxDrop.

Covers the ``send_file`` end-to-end flow: ``config/stss`` (OSS credential
fetch), OSS upload (mocked), and ``push/saveAndPush`` (notification).

Catches three known bugs (per ``gian-didom/onyx-send2boox`` README):
- **resourceType default**: hrw hardcodes ``"txt"`` regardless of the file's
  extension. Test asserts derivation from extension; fix lands in this issue.
- **OSS multipart STS**: ``oss2.resumable_upload`` needs ``ListParts`` perms
  that the STS token doesn't grant; test asserts ``put_object`` usage and is
  marked ``xfail`` — the fix is part of the Phase 1 files-module refactor (#32).
- **NaN:NaN timestamp**: the reader filters out files lacking proper
  timestamps. The fix requires a follow-up ``_bulk_docs`` push to the
  ``<user_uid>-MESSAGE`` channel (per gian-didom); test is marked ``xfail`` —
  proper home is the Phase 4 sync work (#34) wired into the files module (#32).

Added by #5 (Unit test suite — file push to BooxDrop).
"""

import json
import re
import time

import pytest

import boox
from .conftest import TEST_API_BASE


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
    """Register the two HTTP calls send_file always makes."""
    mock_http.get(
        f"{TEST_API_BASE}/config/stss",
        json={"result_code": 0, "data": _STSS_DATA},
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


@pytest.mark.xfail(
    reason=(
        "Multipart-STS fix: switch from oss2.resumable_upload to single-shot "
        "bucket.put_object. resumable_upload requires ListParts perms that "
        "the STS token doesn't grant; bug surfaces for files >10MB (oss2's "
        "default multipart threshold). gian-didom's README claims this fix "
        "but their code still calls resumable_upload. Lands properly with "
        "the Phase 1 files-module refactor (#32)."
    ),
    strict=True,
)
def test_send_file_prefers_put_object_over_resumable_upload(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """OSS upload should be single-shot, not multipart-resumable."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)

    push_ready_client.send_file(str(f))

    assert oss_mocks["bucket"].put_object.called, "put_object not called"
    assert not oss_mocks["resumable_upload"].called, "resumable_upload still in use"


# --------------------------- Timestamp / reader visibility -----------------


@pytest.mark.xfail(
    reason=(
        "NaN-timestamp fix: per gian-didom, a successful push needs a "
        "follow-up POST /neocloud/_bulk_docs to the <user_uid>-MESSAGE "
        "channel with createdAt/updatedAt set to epoch-ms. The reader "
        "filters out files lacking valid timestamps. This is a substantial "
        "addition — requires the sync-gateway primitives from Phase 4 #34 "
        "wired into the files module (#32). Tracked there; do not patch "
        "here on top of hrw's flat boox.py."
    ),
    strict=True,
)
def test_send_file_propagates_valid_timestamp_via_bulk_docs(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """A complete push ends with a _bulk_docs PUT setting valid timestamps."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)
    # Pre-register the (expected) follow-up bulk_docs call.
    mock_http.post(
        f"https://{boox.read_config.__defaults__[0]}/neocloud/_bulk_docs",
        json=[{"id": "doc-id", "rev": "2-fixture"}],
    )

    push_ready_client.send_file(str(f))

    bulk_calls = [c for c in mock_http.calls if "neocloud/_bulk_docs" in c.request.url]
    assert bulk_calls, "send_file should follow saveAndPush with _bulk_docs"
    bulk_body = json.loads(bulk_calls[0].request.body)
    doc = bulk_body["docs"][0]
    assert isinstance(doc["createdAt"], int), "createdAt must be epoch ms int"
    assert isinstance(doc["updatedAt"], int), "updatedAt must be epoch ms int"
    now_ms = time.time() * 1000
    assert abs(doc["updatedAt"] - now_ms) < 5 * 60 * 1000, (
        "updatedAt should be within a few minutes of push time"
    )


# --------------------------- Error paths -----------------------------------


def test_send_file_propagates_oss_failures(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """OSS upload failures bubble up — never swallowed silently."""
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    _stub_stss_and_save(mock_http)
    import oss2

    oss_mocks["resumable_upload"].side_effect = oss2.exceptions.OssError(
        500, {}, "<Error/>", {}
    )
    with pytest.raises(oss2.exceptions.OssError):
        push_ready_client.send_file(str(f))
