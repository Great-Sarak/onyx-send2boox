"""Unit tests for ``boox.sync.binary`` — OSS-STS per-user binary fetch.

Auth pre-conditions are checked before any OSS call, then we mock
``oss2.Bucket`` so individual tests don't need a network or real STS.

The test seam is ``boox.sync.binary.oss2`` — we patch ``oss2.Bucket`` and
``oss2.StsAuth`` there. The STS HTTP call (``config/stss``) is stubbed
through ``mock_http`` so we also exercise the JSON parsing.
"""

from __future__ import annotations

import pytest

import boox.sync.binary as binary_mod
from boox.errors import AuthError

from .conftest import TEST_API_BASE


_STSS_DATA = {
    "AccessKeyId": "STS.FixtureKey",  # pragma: allowlist secret
    "AccessKeySecret": "FixtureSecret",  # pragma: allowlist secret
    "SecurityToken": "FixtureToken",  # pragma: allowlist secret
}


@pytest.fixture
def binary_client(unit_client):
    """Unit client with the OSS-bucket fields ``send_file`` populates."""
    unit_client.bucket_name = "onyx-cloud-test"
    unit_client.endpoint = "oss-test.aliyuncs.com"
    return unit_client


@pytest.fixture
def stub_stss(mock_http):
    """Stub the STS-credential endpoint with a fixture payload."""
    mock_http.get(
        f"{TEST_API_BASE}/config/stss",
        json={"result_code": 0, "data": _STSS_DATA},
    )
    return mock_http


@pytest.fixture
def oss_mocks(mocker):
    """Patch ``oss2`` inside ``boox.sync.binary``.

    Returns a dict with the patched ``StsAuth`` and ``Bucket`` plus the
    ``bucket_instance`` mock that ``Bucket(...)`` returns. Tests configure
    ``bucket_instance.get_object`` per scenario.
    """
    sts_auth = mocker.patch.object(binary_mod.oss2, "StsAuth")
    bucket_instance = mocker.MagicMock()
    bucket_cls = mocker.patch.object(
        binary_mod.oss2, "Bucket", return_value=bucket_instance
    )
    return {
        "StsAuth": sts_auth,
        "Bucket": bucket_cls,
        "bucket_instance": bucket_instance,
    }


def _stub_get_object(oss_mocks, payload: bytes):
    obj = oss_mocks["bucket_instance"].get_object.return_value
    obj.read.return_value = payload


# --------------------------- happy paths ----------------------------------


def test_fetch_note_template_returns_bytes(binary_client, stub_stss, oss_mocks):
    _stub_get_object(oss_mocks, b'{"template": "fixture"}')

    out = binary_mod.fetch_note_template(
        binary_client, "user-uid", "note-id", "page-uuid"
    )

    assert out == b'{"template": "fixture"}'
    key = oss_mocks["bucket_instance"].get_object.call_args.args[0]
    assert key == "user-uid/note/note-id/template/json/page-uuid.template_json"


def test_fetch_note_point_group_returns_bytes(binary_client, stub_stss, oss_mocks):
    _stub_get_object(oss_mocks, b"\x00\x01\x02point-binary")

    out = binary_mod.fetch_note_point_group(
        binary_client, "user-uid", "note-id", "page-uuid", "pg-uuid"
    )

    assert out == b"\x00\x01\x02point-binary"


def test_fetch_book_thumbnail_returns_bytes(binary_client, stub_stss, oss_mocks):
    _stub_get_object(oss_mocks, b"\x89PNG\r\n\x1a\n")

    out = binary_mod.fetch_book_thumbnail(binary_client, "user-uid", "book-uuid")

    assert out == b"\x89PNG\r\n\x1a\n"
    key = oss_mocks["bucket_instance"].get_object.call_args.args[0]
    assert key == "user-uid/reader/book-uuid/thumbnail/book-uuid.png"


def test_fetch_book_thumbnail_honors_ext(binary_client, stub_stss, oss_mocks):
    _stub_get_object(oss_mocks, b"jpegbytes")

    binary_mod.fetch_book_thumbnail(binary_client, "user-uid", "book-uuid", ext="jpg")

    key = oss_mocks["bucket_instance"].get_object.call_args.args[0]
    assert key.endswith("/thumbnail/book-uuid.jpg")


def test_fetch_book_file_returns_bytes(binary_client, stub_stss, oss_mocks):
    _stub_get_object(oss_mocks, b"%PDF-1.7 fixture")

    out = binary_mod.fetch_book_file(
        binary_client, "user-uid", "book-uuid", ext="pdf"
    )

    assert out == b"%PDF-1.7 fixture"
    key = oss_mocks["bucket_instance"].get_object.call_args.args[0]
    assert key == "user-uid/reader/book-uuid/book/book-uuid.pdf"


# --------------------------- URL encoding ---------------------------------


def test_point_group_key_url_encodes_hashes(binary_client, stub_stss, oss_mocks):
    """The two ``#`` chars must be ``%23`` on the OSS key.

    Unencoded ``#`` would be treated as a URL fragment marker on the wire,
    silently truncating the request path at the first hash.
    """
    _stub_get_object(oss_mocks, b"x")

    binary_mod.fetch_note_point_group(
        binary_client,
        "user-uid",
        "note-id",
        "22baca72-5b44-466d-8e6e-cba1806773a8",
        "9a3f9ea3-e845-4960-b527-1440c4862313",
    )

    key = oss_mocks["bucket_instance"].get_object.call_args.args[0]
    assert "#" not in key, f"Raw '#' found in OSS key: {key!r}"
    assert key.count("%23") == 2, f"Expected two %23 escapes in {key!r}"
    # Spot-check the shape too.
    assert key == (
        "user-uid/note/note-id/point/"
        "22baca72-5b44-466d-8e6e-cba1806773a8"
        "%23"
        "9a3f9ea3-e845-4960-b527-1440c4862313"
        "%23"
        "points"
    )


# --------------------------- 404 handling ---------------------------------


def test_fetch_book_thumbnail_returns_none_on_404(
    binary_client, stub_stss, oss_mocks
):
    """Sideloaded books often have no cloud thumbnail — return None, not raise."""
    from oss2.exceptions import NoSuchKey

    oss_mocks["bucket_instance"].get_object.side_effect = NoSuchKey(
        404, {}, b"<Error><Code>NoSuchKey</Code></Error>", {}
    )

    out = binary_mod.fetch_book_thumbnail(binary_client, "user-uid", "missing-book")

    assert out is None


def test_fetch_book_file_returns_none_on_404(binary_client, stub_stss, oss_mocks):
    from oss2.exceptions import NoSuchKey

    oss_mocks["bucket_instance"].get_object.side_effect = NoSuchKey(
        404, {}, b"<Error><Code>NoSuchKey</Code></Error>", {}
    )

    out = binary_mod.fetch_book_file(
        binary_client, "user-uid", "missing-book", ext="epub"
    )

    assert out is None


def test_fetch_book_thumbnail_also_handles_generic_notfound(
    binary_client, stub_stss, oss_mocks
):
    """``oss2.exceptions.NotFound`` (super of NoSuchKey) also maps to None."""
    from oss2.exceptions import NotFound

    oss_mocks["bucket_instance"].get_object.side_effect = NotFound(
        404, {}, b"<Error/>", {}
    )

    out = binary_mod.fetch_book_thumbnail(binary_client, "user-uid", "book-uuid")

    assert out is None


# --------------------------- STS auth construction ------------------------


def test_sts_auth_built_from_config_stss(binary_client, stub_stss, oss_mocks):
    """Confirm the STS values flow from ``config/stss`` into ``oss2.StsAuth``."""
    _stub_get_object(oss_mocks, b"x")

    binary_mod.fetch_note_template(binary_client, "u", "n", "p")

    oss_mocks["StsAuth"].assert_called_once_with(
        "STS.FixtureKey",  # pragma: allowlist secret
        "FixtureSecret",  # pragma: allowlist secret
        "FixtureToken",  # pragma: allowlist secret
    )
    bucket_args = oss_mocks["Bucket"].call_args.args
    # (auth, endpoint, bucket_name)
    assert bucket_args[1] == "oss-test.aliyuncs.com"
    assert bucket_args[2] == "onyx-cloud-test"


def test_missing_bearer_token_raises_auth_error(binary_client, oss_mocks):
    """Without ``client.token`` we can't mint STS — fail before hitting OSS."""
    binary_client.token = False

    with pytest.raises(AuthError):
        binary_mod.fetch_note_template(binary_client, "u", "n", "p")

    oss_mocks["Bucket"].assert_not_called()
    oss_mocks["StsAuth"].assert_not_called()
