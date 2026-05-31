"""Targeted test — OSS object key format (double-dot edge case).

The original ``send_file`` in hrw's library built the OSS object key as::

    extension = os.path.splitext(filename)[1]     # e.g. '.pdf'
    remotename = f'{userid}/push/{uuid}.{extension}'

``os.path.splitext`` returns the extension *including* the leading dot, so
the f-string concatenation produced ``<userid>/push/<uuid>..pdf`` — two
dots. The web UI's 2026-05-31 BooxDrop HAR confirms the correct format is
single-dot::

    /659852caf8952c1372f222ab/push/bbe5890904f44caba302ed898c20392f.pdf

This is its own targeted issue (separate from the broader push test suite
in #5) because the broader test asserts payload semantics, not the exact
OSS key string format — the bug wouldn't naturally surface without a
focused regex check.

Added by #7 (Targeted test — OSS object key format).
"""

import json
import re

import pytest

import boox
from .conftest import TEST_API_BASE, TEST_NEOCLOUD_BASE


_STSS_DATA = {
    "AccessKeyId": "STS.FixtureKey",  # pragma: allowlist secret
    "AccessKeySecret": "FixtureSecret",  # pragma: allowlist secret
    "SecurityToken": "FixtureToken",  # pragma: allowlist secret
}


@pytest.fixture
def push_ready_client(unit_client):
    unit_client.userid = "user-uid-fixture"
    unit_client.bucket_name = "onyx-cloud-test"
    unit_client.endpoint = "oss-test.aliyuncs.com"
    return unit_client


@pytest.fixture
def oss_mocks(mocker):
    bucket_instance = mocker.MagicMock()
    mocker.patch.object(boox.oss2, "Bucket", return_value=bucket_instance)
    mocker.patch.object(boox.oss2, "Auth")
    resumable = mocker.patch.object(boox.oss2, "resumable_upload")
    return {"bucket": bucket_instance, "resumable_upload": resumable}


def _stub_http(mock_http):
    mock_http.get(
        f"{TEST_API_BASE}/config/stss",
        json={"result_code": 0, "data": _STSS_DATA},
    )
    mock_http.post(
        f"{TEST_NEOCLOUD_BASE}/_bulk_docs",
        json=[{"id": "doc-id", "rev": "1-fixture"}],
        status=201,
    )
    mock_http.post(
        f"{TEST_API_BASE}/push/saveAndPush",
        json={"result_code": 0, "data": "ok"},
    )


def _captured_oss_key(mock_http, oss_mocks):
    """Extract the OSS object key from either saveAndPush body or
    resumable_upload args — they should agree."""
    save_call = next(c for c in mock_http.calls if "saveAndPush" in c.request.url)
    payload = json.loads(save_call.request.body)["data"]
    key_from_save = payload["resourceKey"]
    # Sanity check: same key was passed to oss2.resumable_upload
    if oss_mocks["resumable_upload"].called:
        args, _ = oss_mocks["resumable_upload"].call_args
        key_from_oss = args[1]  # (bucket, key, filepath)
        assert key_from_save == key_from_oss
    return key_from_save


@pytest.mark.parametrize(
    "filename,expected_ext",
    [
        ("book.pdf", ".pdf"),
        ("story.epub", ".epub"),
        ("comic.cbz", ".cbz"),
        ("manual.azw3", ".azw3"),
        ("Foo Bar v2.5.pdf", ".pdf"),  # multiple dots — only the last counts
        ("doc.with.many.dots.txt", ".txt"),  # ditto
    ],
)
def test_oss_key_has_single_dot_before_extension(
    mock_http, push_ready_client, oss_mocks, tmp_path, filename, expected_ext
):
    """``<user_uid>/push/<uuid><single-dot><ext>``, NOT ``<uuid>..<ext>``."""
    f = tmp_path / filename
    f.write_bytes(b"content")
    _stub_http(mock_http)

    push_ready_client.send_file(str(f))

    key = _captured_oss_key(mock_http, oss_mocks)
    # Match exactly: <user_uid>/push/<uuid>.<ext> — single dot.
    assert re.match(
        rf"^user-uid-fixture/push/[0-9a-f-]+{re.escape(expected_ext)}$",
        key,
    ), f"OSS key format wrong: {key!r} (expected single-dot ending {expected_ext})"
    # Explicit: no double-dot anywhere in the key.
    assert ".." not in key, f"Double-dot in OSS key: {key!r}"


def test_oss_key_dotless_filename_has_no_trailing_dot(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``README`` produces a key with no trailing dot and no extra suffix."""
    f = tmp_path / "README"
    f.write_bytes(b"content")
    _stub_http(mock_http)

    push_ready_client.send_file(str(f))

    key = _captured_oss_key(mock_http, oss_mocks)
    assert re.match(r"^user-uid-fixture/push/[0-9a-f-]+$", key), (
        f"Dotless file produced unexpected OSS key: {key!r}"
    )
    assert not key.endswith("."), f"Trailing dot in OSS key for dotless file: {key!r}"


def test_oss_key_dotfile_handled_as_dotless(
    mock_http, push_ready_client, oss_mocks, tmp_path
):
    """``.hidden`` (leading dot, no other extension) — os.path.splitext
    returns ('.hidden', '') on Unix, so this is functionally dotless."""
    f = tmp_path / ".hidden"
    f.write_bytes(b"content")
    _stub_http(mock_http)

    push_ready_client.send_file(str(f))

    key = _captured_oss_key(mock_http, oss_mocks)
    # Either no extension OR ".hidden" — depends on splitext semantics on
    # this platform. Assert no double-dot regardless.
    assert ".." not in key, f"Double-dot in OSS key for dotfile: {key!r}"
