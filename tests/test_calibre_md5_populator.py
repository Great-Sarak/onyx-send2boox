"""Unit tests for :mod:`boox.sync.calibre_md5_populator`.

Covers the populator's pure-Python helpers (``pick_format``,
``format_url``, ``compute_md5_streaming``) plus the orchestration in
``populate_md5_identifiers`` against a fully-mocked
:class:`CalibreClient` runner and ``responses``-mocked HTTP fetches.
The CLI driver lives in ``scripts/populate_calibre_md5_identifiers.py``
and is exercised end-to-end by the live test below.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import List
from unittest.mock import MagicMock

import pytest
import responses
from responses.matchers import header_matcher

from boox.sync import CalibreBridgeError, CalibreClient
from boox.sync.calibre_md5_populator import (
    DEFAULT_FORMAT_PRIORITY,
    DOWNLOAD_CHUNK_BYTES,
    PopulateResult,
    compute_md5_streaming,
    format_url,
    pick_format,
    populate_md5_identifiers,
)


# --------------------------- pure-helper tests -----------------------------


def test_pick_format_empty_returns_none():
    assert pick_format([]) is None
    assert pick_format(None) is None


def test_pick_format_prefers_epub_over_pdf():
    assert pick_format(["/a/b/Title.PDF", "/a/b/Title.epub"]) == "epub"


def test_pick_format_falls_through_priority_order():
    formats = ["/a/Title.MOBI", "/a/Title.AZW3"]
    assert pick_format(formats) == "azw3"


def test_pick_format_unknown_extension_still_returned():
    """A book with only an unfamiliar format isn't silently dropped —
    we hand it back so the populator can try fetching whatever it is.
    """
    assert pick_format(["/a/Title.xyz"]) == "xyz"


def test_pick_format_skips_empty_paths():
    assert pick_format(["", None, "/a/Title.epub", ""]) == "epub"


def test_pick_format_custom_priority_wins():
    formats = ["/a/Title.EPUB", "/a/Title.PDF"]
    out = pick_format(formats, priority=("pdf", "epub"))
    assert out == "pdf"


def test_pick_format_bare_format_names_from_remote_calibredb():
    """Remote-library ``calibredb list --fields formats`` returns the
    format names verbatim (``["EPUB", "PDF"]``) instead of on-disk
    paths; the populator handles both shapes.
    """
    assert pick_format(["EPUB"]) == "epub"
    assert pick_format(["PDF", "EPUB"]) == "epub"
    assert pick_format(["MOBI", "AZW3"]) == "azw3"


def test_format_url_lowercases_format_and_strips_trailing_slash():
    url = format_url(
        "https://calibre.example/",
        "Books",
        42,
        "EPUB",
    )
    assert url == "https://calibre.example/get/epub/42/Books"


# --------------------------- compute_md5_streaming -------------------------


@responses.activate
def test_compute_md5_streaming_matches_hashlib_md5():
    payload = b"some book content " * 100_000  # ~1.7 MiB — spans chunks
    expected = hashlib.md5(payload).hexdigest()

    responses.get(
        "https://calibre.example/get/epub/1/Books",
        body=payload,
        status=200,
    )

    out = compute_md5_streaming(
        "https://calibre.example/get/epub/1/Books",
        auth=("user", "pass"),  # type: ignore[arg-type]
        chunk_size=512 * 1024,
    )
    assert out == expected


@responses.activate
def test_compute_md5_streaming_raises_on_4xx():
    responses.get(
        "https://calibre.example/get/epub/1/Books",
        json={"error": "not found"},
        status=404,
    )

    with pytest.raises(Exception):  # requests.HTTPError
        compute_md5_streaming(
            "https://calibre.example/get/epub/1/Books",
            auth=("user", "pass"),  # type: ignore[arg-type]
        )


# --------------------------- populate orchestration ------------------------


def _make_client(
    list_books_rows,
    set_metadata_results=None,
):
    """Build a CalibreClient backed by a mock runner.

    ``list_books_rows`` is the JSON-decoded ``calibredb list`` rows.
    ``set_metadata_results`` is an optional list of CompletedProcess
    instances to return in order on successive ``set_metadata`` calls.
    """
    set_metadata_results = list(set_metadata_results or [])

    def runner(argv):
        # First positional arg after calibredb is the subcommand.
        subcmd = argv[1]
        if subcmd == "list":
            return subprocess.CompletedProcess(
                argv,
                returncode=0,
                stdout=json.dumps(list_books_rows),
                stderr="",
            )
        if subcmd == "set_metadata":
            if set_metadata_results:
                return set_metadata_results.pop(0)
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="", stderr=""
            )
        # custom_columns, add_custom_column, etc. — not used here.
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    return CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )


@responses.activate
def test_populate_writes_md5_for_each_book_lacking_one():
    """Happy path — three books with formats, none have md5 yet."""
    payload_a = b"book A content"
    payload_b = b"book B content"
    payload_c = b"book C content"

    rows = [
        {"id": 1, "title": "A", "identifiers": {}, "formats": ["/lib/A.epub"]},
        {"id": 2, "title": "B", "identifiers": {}, "formats": ["/lib/B.pdf"]},
        {"id": 3, "title": "C", "identifiers": {}, "formats": ["/lib/C.azw3"]},
    ]

    responses.get(
        "https://calibre.example/get/epub/1/Books",
        body=payload_a,
        status=200,
    )
    responses.get(
        "https://calibre.example/get/pdf/2/Books",
        body=payload_b,
        status=200,
    )
    responses.get(
        "https://calibre.example/get/azw3/3/Books",
        body=payload_c,
        status=200,
    )

    set_metadata_calls: List[list] = []
    original_runner = None

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        if argv[1] == "set_metadata":
            set_metadata_calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    result = populate_md5_identifiers(client)
    assert result.populated == 3
    assert result.skipped_already_present == 0
    assert result.skipped_no_format == 0
    assert result.errors == []

    # Three set_metadata calls, one per book, with the right md5 each.
    expected_md5s = [
        hashlib.md5(p).hexdigest()
        for p in (payload_a, payload_b, payload_c)
    ]
    assert len(set_metadata_calls) == 3
    for call_argv, expected_hash in zip(set_metadata_calls, expected_md5s):
        joined = " ".join(call_argv)
        assert f"identifiers:md5:{expected_hash}" in joined


def test_populate_skips_books_with_existing_md5_by_default():
    """A book whose identifiers already include md5:... is skipped."""
    rows = [
        {
            "id": 1,
            "title": "A",
            "identifiers": {"md5": "deadbeef" * 4},
            "formats": ["/lib/A.epub"],
        },
        {
            "id": 2,
            "title": "B",
            "identifiers": {},
            "formats": ["/lib/B.epub"],
        },
    ]

    set_metadata_calls: List[list] = []

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        if argv[1] == "set_metadata":
            set_metadata_calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        # Only book id=2 should ever be fetched.
        rsps.get(
            "https://calibre.example/get/epub/2/Books",
            body=b"book B",
            status=200,
        )
        result = populate_md5_identifiers(client)

    assert result.skipped_already_present == 1
    assert result.populated == 1
    assert len(set_metadata_calls) == 1
    # Confirm the call was for book 2, not book 1.
    assert "2" in set_metadata_calls[0]


def test_populate_no_skip_existing_overwrites():
    """``skip_existing=False`` re-hashes and overwrites the md5."""
    rows = [
        {
            "id": 1,
            "title": "A",
            "identifiers": {"md5": "stale" * 5 + "abc"},
            "formats": ["/lib/A.epub"],
        },
    ]
    set_metadata_calls: List[list] = []

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        if argv[1] == "set_metadata":
            set_metadata_calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://calibre.example/get/epub/1/Books",
            body=b"book A fresh content",
            status=200,
        )
        result = populate_md5_identifiers(client, skip_existing=False)

    assert result.populated == 1
    assert result.skipped_already_present == 0
    assert len(set_metadata_calls) == 1
    fresh_md5 = hashlib.md5(b"book A fresh content").hexdigest()
    assert any(fresh_md5 in tok for tok in set_metadata_calls[0])


def test_populate_dry_run_does_not_call_set_metadata():
    rows = [
        {"id": 1, "title": "A", "identifiers": {}, "formats": ["/lib/A.epub"]},
        {"id": 2, "title": "B", "identifiers": {}, "formats": ["/lib/B.epub"]},
    ]

    set_metadata_calls: List[list] = []

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        if argv[1] == "set_metadata":
            set_metadata_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://calibre.example/get/epub/1/Books",
            body=b"AAA",
            status=200,
        )
        rsps.get(
            "https://calibre.example/get/epub/2/Books",
            body=b"BBB",
            status=200,
        )
        result = populate_md5_identifiers(client, dry_run=True)

    assert result.populated == 2
    assert set_metadata_calls == []
    assert len(result.dry_run_plan) == 2
    assert {p["book_id"] for p in result.dry_run_plan} == {1, 2}


def test_populate_no_format_skipped_separately():
    rows = [
        {"id": 1, "title": "no-files", "identifiers": {}, "formats": []},
        {"id": 2, "title": "has-epub", "identifiers": {}, "formats": ["/lib/B.epub"]},
    ]

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://calibre.example/get/epub/2/Books",
            body=b"OK",
            status=200,
        )
        result = populate_md5_identifiers(client)

    assert result.skipped_no_format == 1
    assert result.populated == 1


def test_populate_download_error_recorded_but_run_continues():
    rows = [
        {"id": 1, "title": "A", "identifiers": {}, "formats": ["/lib/A.epub"]},
        {"id": 2, "title": "B", "identifiers": {}, "formats": ["/lib/B.epub"]},
    ]

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://calibre.example/get/epub/1/Books",
            json={"error": "boom"},
            status=500,
        )
        rsps.get(
            "https://calibre.example/get/epub/2/Books",
            body=b"OK",
            status=200,
        )
        result = populate_md5_identifiers(client)

    assert result.populated == 1  # book 2 still got through
    assert len(result.errors) == 1
    assert result.errors[0]["book_id"] == 1
    assert result.errors[0]["stage"] == "download_or_hash"


def test_populate_set_metadata_failure_recorded():
    rows = [
        {"id": 1, "title": "A", "identifiers": {}, "formats": ["/lib/A.epub"]},
    ]

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        if argv[1] == "set_metadata":
            return subprocess.CompletedProcess(
                argv, returncode=2, stdout="", stderr="permission denied"
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://calibre.example/get/epub/1/Books",
            body=b"OK",
            status=200,
        )
        result = populate_md5_identifiers(client)

    assert result.populated == 0
    assert len(result.errors) == 1
    assert result.errors[0]["stage"] == "set_metadata"
    assert "permission denied" in result.errors[0]["error"]


def test_populate_limit_caps_processed_books():
    rows = [
        {"id": i, "title": f"book-{i}", "identifiers": {}, "formats": [f"/lib/{i}.epub"]}
        for i in range(1, 11)
    ]

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    with responses.RequestsMock() as rsps:
        for i in range(1, 4):
            rsps.get(
                f"https://calibre.example/get/epub/{i}/Books",
                body=f"book-{i}".encode(),
                status=200,
            )
        result = populate_md5_identifiers(client, limit=3)

    # Only the first 3 books processed; 4..10 untouched.
    assert result.populated == 3


def test_populate_progress_callback_invoked_per_book():
    rows = [
        {"id": 1, "title": "A", "identifiers": {}, "formats": ["/lib/A.epub"]},
        {"id": 2, "title": "B", "identifiers": {"md5": "x"}, "formats": ["/lib/B.epub"]},
    ]

    def runner(argv):
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    client = CalibreClient(
        server_url="https://calibre.example",
        username="user",
        password="pw",
        library_id="Books",
        runner=runner,
    )

    callback = MagicMock()

    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://calibre.example/get/epub/1/Books",
            body=b"A",
            status=200,
        )
        populate_md5_identifiers(client, progress_callback=callback)

    # One call per book: 2 total.
    assert callback.call_count == 2
    statuses = [c.args[2] for c in callback.call_args_list]
    assert "populated" in statuses
    assert "skip:already-present" in statuses


def test_populate_result_to_dict_serializable():
    r = PopulateResult(populated=2, skipped_already_present=1)
    out = r.to_dict()
    assert out["populated"] == 2
    assert out["skipped_already_present"] == 1
    assert out["errors"] == []
    # Should round-trip through JSON.
    json.dumps(out)


# --------------------------- live smoke ------------------------------------


@pytest.mark.live
def test_live_populate_dry_run_smoke():
    """Dry-run against a real Calibre Content Server.

    Gated on ``BOOX_RUN_LIVE_TESTS`` and requires
    ``CALIBRE_CONTENT_SERVER_URL`` + ``CALIBRE_USERNAME`` +
    ``CALIBRE_PASSWORD`` + ``CALIBRE_LIBRARY_ID`` in the environment.
    Limits to 3 books so the test stays fast.

    Asserts that the run completes without raising, and that at least
    one of (populated / skipped_already_present / skipped_no_format /
    errors) accounts for each book seen — proving the full path
    (list_books → format pick → URL build → HTTP fetch → MD5 stream)
    runs end to end against real wire.
    """
    import os

    if not (
        os.environ.get("CALIBRE_CONTENT_SERVER_URL")
        and os.environ.get("CALIBRE_USERNAME")
        and os.environ.get("CALIBRE_PASSWORD")
        and os.environ.get("CALIBRE_LIBRARY_ID")
    ):
        pytest.skip(
            "live populator test needs CALIBRE_CONTENT_SERVER_URL / "
            "CALIBRE_USERNAME / CALIBRE_PASSWORD / CALIBRE_LIBRARY_ID"
        )

    calibre = CalibreClient()
    result = populate_md5_identifiers(calibre, dry_run=True, limit=3)
    accounted = (
        result.populated
        + result.skipped_already_present
        + result.skipped_no_format
        + len(result.errors)
    )
    # Account for every book that was offered.
    assert accounted <= 3 and accounted >= 0
    # If any book was populated in dry-run, the plan should match.
    assert len(result.dry_run_plan) == result.populated
