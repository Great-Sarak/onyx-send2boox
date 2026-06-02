"""Unit tests for ``boox.sync.calibre_bridge`` — Calibre bridge (#39).

We don't talk to a real ``calibredb``: the :class:`CalibreClient` takes
a ``runner`` injection seam that returns canned
``subprocess.CompletedProcess`` instances. The :class:`_FakeCalibre`
helper keeps a scripted queue keyed by ``calibredb`` subcommand and
records every call so tests can assert the argv shape (including the
URL+creds form) without re-implementing the parser.

Live smoke (``@pytest.mark.live``) talks to the real fleet Calibre via
the env-injected creds; it's gated by ``BOOX_RUN_LIVE_TESTS`` and
additionally requires ``BOOX_CALIBRE_TEST_MD5`` for the MD5 we expect
to match.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from boox.sync import LocalStore
from boox.sync.calibre_bridge import (
    BookMatch,
    CalibreBook,
    CalibreBridgeError,
    CalibreClient,
    MatchResult,
    READING_STATUS_MAP,
    SyncSummary,
    map_reading_status,
    match_books,
    ms_to_iso8601,
    sync_reading_state,
)
from boox.sync.reader import _LOCAL_CHANNEL


# --------------------------- helpers ---------------------------------------


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakeCalibre:
    """Scripted ``calibredb`` runner.

    ``responses`` is keyed by subcommand name; the value is either a
    single ``CompletedProcess`` (replayed for every invocation) or a
    list, popped in order.
    """

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses: Dict[str, Any] = {k: v for k, v in responses.items()}
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        subcmd = argv[1] if len(argv) > 1 else ""
        slot = self.responses.get(subcmd)
        if slot is None:
            return _cp(returncode=0, stdout="[]")
        if isinstance(slot, list):
            if not slot:
                return _cp(returncode=0, stdout="[]")
            return slot.pop(0)
        return slot

    def subcmd_calls(self, subcmd: str) -> List[List[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == subcmd]


def _client(runner: _FakeCalibre) -> CalibreClient:
    return CalibreClient(
        server_url="http://calibre.local:8080",
        username="u",
        password="p",  # pragma: allowlist secret
        library_id="Books",
        runner=runner,
    )


def _store_with_books(books: Sequence[Mapping[str, Any]]) -> LocalStore:
    store = LocalStore(":memory:")
    for body in books:
        store.upsert_doc(_LOCAL_CHANNEL, body["_id"], body["_rev"], body)
    return store


def _book_body(
    doc_id: str,
    md5: Optional[str],
    *,
    rev: str = "1-abc",
    name: str = "A Book",
    last_access: Optional[int] = 1748000000000,
    progress: Optional[str] = "3/5",
    reading_status: Optional[int] = 1,
    rating: Optional[int] = 4,
    favorite: Optional[int] = 0,
) -> Dict[str, Any]:
    extra = {"backend": {"md5": md5}} if md5 is not None else {}
    return {
        "_id": doc_id,
        "_rev": rev,
        "UUID": doc_id.split("#")[-1],
        "name": name,
        "lastAccess": last_access,
        "progress": progress,
        "readingStatus": reading_status,
        "rating": rating,
        "favorite": favorite,
        "extraAttributes": json.dumps(extra) if extra else "",
    }


# --------------------------- helpers/units ---------------------------------


def test_ms_to_iso8601_typical():
    out = ms_to_iso8601(1748000000000)
    assert out == "2025-05-23T11:33:20+00:00"


def test_ms_to_iso8601_none_and_zero():
    assert ms_to_iso8601(None) is None
    assert ms_to_iso8601(0) is None
    assert ms_to_iso8601("not-a-number") is None


def test_map_reading_status_known_and_unknown():
    assert map_reading_status(0) == "unread"
    assert map_reading_status(1) == "reading"
    assert map_reading_status(2) == "finished"
    assert map_reading_status(99) == "unknown:99"
    assert map_reading_status(None) is None


def test_reading_status_map_constant():
    # If someone re-shapes the table, the test should yell.
    assert READING_STATUS_MAP[2] == "finished"


# --------------------------- CalibreClient ---------------------------------


def test_calibre_client_env_missing(monkeypatch):
    for var in (
        "CALIBRE_CONTENT_SERVER_URL",
        "CALIBRE_USERNAME",
        "CALIBRE_PASSWORD",
        "CALIBRE_LIBRARY_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(CalibreBridgeError):
        CalibreClient()


def test_calibre_client_with_library_url_shape():
    fake = _FakeCalibre({})
    c = _client(fake)
    assert c.with_library == "http://calibre.local:8080/#Books"


def test_calibre_client_list_books_parses_identifiers():
    rows = [
        {"id": 1, "title": "T1", "authors": "A", "identifiers": {"md5": "DEAD"}},
        {"id": 2, "title": "T2", "authors": "B", "identifiers": "isbn:x,md5:beef"},
        {"id": 3, "title": "T3", "authors": "C", "identifiers": {}},
    ]
    fake = _FakeCalibre({"list": _cp(stdout=json.dumps(rows))})
    c = _client(fake)
    books = c.list_books()
    assert [b.id for b in books] == [1, 2, 3]
    assert books[0].md5 == "dead"
    assert books[1].md5 == "beef"
    assert books[2].md5 is None
    # Sanity: argv carried URL+creds form.
    argv = fake.calls[0]
    assert "--with-library" in argv
    assert "http://calibre.local:8080/#Books" in argv
    assert "--username" in argv
    assert "--password" in argv


def test_calibre_client_list_books_failure():
    fake = _FakeCalibre({"list": _cp(returncode=1, stderr="boom")})
    c = _client(fake)
    with pytest.raises(CalibreBridgeError):
        c.list_books()


def test_add_custom_column_already_exists_is_success():
    fake = _FakeCalibre({
        "add_custom_column": _cp(returncode=1, stderr="error: column already exists"),
    })
    c = _client(fake)
    c.add_custom_column("last_read", "Last Read", "datetime")  # no raise


def test_add_custom_column_other_failure_raises():
    fake = _FakeCalibre({
        "add_custom_column": _cp(returncode=1, stderr="permission denied"),
    })
    c = _client(fake)
    with pytest.raises(CalibreBridgeError):
        c.add_custom_column("last_read", "Last Read", "datetime")


def test_set_metadata_skips_none_values():
    fake = _FakeCalibre({"set_metadata": _cp()})
    c = _client(fake)
    c.set_metadata(42, {"#last_read": None, "#read_progress": "3/5"})
    argv = fake.calls[0]
    # Only the non-None field made it onto the command line.
    joined = " ".join(argv)
    assert "#read_progress:3/5" in joined
    assert "#last_read" not in joined


def test_set_metadata_all_none_makes_no_call():
    fake = _FakeCalibre({"set_metadata": _cp(returncode=99)})
    c = _client(fake)
    c.set_metadata(42, {"#last_read": None})
    assert fake.calls == []


# --------------------------- match_books -----------------------------------


def test_match_books_md5_match():
    store = _store_with_books([_book_body("user#A", md5="abc")])
    fake = _FakeCalibre({
        "list": _cp(stdout=json.dumps(
            [{"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}]
        )),
    })
    result = match_books(store, _client(fake))
    assert len(result.matched) == 1
    assert result.matched[0].boox_book_id == "user#A"
    assert result.matched[0].calibre_book_id == 7
    assert result.matched[0].md5 == "abc"
    assert result.unmatched_boox == []
    assert result.unmatched_calibre == []


def test_match_books_no_md5_in_boox():
    store = _store_with_books([_book_body("user#A", md5=None)])
    fake = _FakeCalibre({"list": _cp(stdout="[]")})
    result = match_books(store, _client(fake))
    assert result.matched == []
    assert len(result.unmatched_boox) == 1
    assert result.unmatched_boox[0].id == "user#A"


def test_match_books_md5_not_in_calibre():
    store = _store_with_books([_book_body("user#A", md5="abc")])
    fake = _FakeCalibre({
        "list": _cp(stdout=json.dumps(
            [{"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "different"}}]
        )),
    })
    result = match_books(store, _client(fake))
    assert result.matched == []
    assert len(result.unmatched_boox) == 1
    assert len(result.unmatched_calibre) == 1
    assert result.unmatched_calibre[0].id == 7


def test_match_books_case_insensitive_md5():
    store = _store_with_books([_book_body("user#A", md5="ABCDEF")])
    fake = _FakeCalibre({
        "list": _cp(stdout=json.dumps(
            [{"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abcdef"}}]
        )),
    })
    result = match_books(store, _client(fake))
    assert len(result.matched) == 1


# --------------------------- sync_reading_state ----------------------------


def test_sync_custom_column_auto_create_when_missing():
    store = _store_with_books([_book_body("user#A", md5="abc")])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=""),  # no existing columns
        "add_custom_column": _cp(),
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),  # match_books call
            _cp(stdout="[]"),  # get_book_fields current state — empty row
        ],
        "set_metadata": _cp(),
    })
    c = _client(fake)
    summary = sync_reading_state(store, c)
    assert summary.updated == 1
    add_calls = fake.subcmd_calls("add_custom_column")
    assert len(add_calls) == 5  # five custom columns
    # Verify the names came through; first positional after auth flags
    # is the column name (auth uses 6 tokens: --with-library, url,
    # --username, u, --password, p).
    names = {call[8] for call in add_calls}
    assert names == {"last_read", "read_progress", "reading_status", "boox_rating", "favorite"}


def test_sync_custom_column_skips_when_existing():
    existing_listing = "\n".join([
        "Last Read (#last_read, datetime)",
        "Read Progress (#read_progress, text)",
        "Reading Status (#reading_status, text)",
        "Boox Rating (#boox_rating, int)",
        "Favorite (#favorite, bool)",
    ])
    store = _store_with_books([_book_body("user#A", md5="abc")])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=existing_listing),
        "list": [
            _cp(stdout=json.dumps(
                [{"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}]
            )),
            _cp(stdout="[]"),
        ],
        "set_metadata": _cp(),
    })
    c = _client(fake)
    summary = sync_reading_state(store, c)
    assert summary.updated == 1
    assert fake.subcmd_calls("add_custom_column") == []


def test_sync_idempotent_second_run_no_writes():
    book_body = _book_body("user#A", md5="abc")
    store = _store_with_books([book_body])

    # Compute the planned values the way the bridge does, so the
    # "current" row coming back from Calibre matches exactly.
    planned_current = {
        "#last_read": "2025-05-23T11:33:20+00:00",
        "#read_progress": "3/5",
        "#reading_status": "reading",
        "#boox_rating": 4,
        "#favorite": False,
    }

    fake = _FakeCalibre({
        "custom_columns": _cp(stdout="Last Read (#last_read, datetime)\n"
                                     "Read Progress (#read_progress, text)\n"
                                     "Reading Status (#reading_status, text)\n"
                                     "Boox Rating (#boox_rating, int)\n"
                                     "Favorite (#favorite, bool)"),
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),
            _cp(stdout=json.dumps([planned_current])),
        ],
    })
    c = _client(fake)
    summary = sync_reading_state(store, c)
    assert summary.updated == 0
    assert summary.unchanged == 1
    assert fake.subcmd_calls("set_metadata") == []


def test_sync_status_enum_finished():
    body = _book_body("user#A", md5="abc", reading_status=2)
    store = _store_with_books([body])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=""),
        "add_custom_column": _cp(),
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),
            _cp(stdout="[]"),
        ],
        "set_metadata": _cp(),
    })
    c = _client(fake)
    sync_reading_state(store, c)
    set_calls = fake.subcmd_calls("set_metadata")
    assert set_calls, "expected at least one set_metadata call"
    joined = " ".join(set_calls[0])
    assert "#reading_status:finished" in joined


def test_sync_status_enum_unknown_preserves_raw():
    body = _book_body("user#A", md5="abc", reading_status=99)
    store = _store_with_books([body])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=""),
        "add_custom_column": _cp(),
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),
            _cp(stdout="[]"),
        ],
        "set_metadata": _cp(),
    })
    c = _client(fake)
    sync_reading_state(store, c)
    set_calls = fake.subcmd_calls("set_metadata")
    joined = " ".join(set_calls[0])
    assert "#reading_status:unknown:99" in joined


def test_sync_dry_run_no_writes():
    body = _book_body("user#A", md5="abc")
    store = _store_with_books([body])
    fake = _FakeCalibre({
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),
            _cp(stdout="[]"),
        ],
    })
    c = _client(fake)
    summary = sync_reading_state(store, c, dry_run=True)
    assert summary.updated == 1
    # No write calls of any kind.
    assert fake.subcmd_calls("set_metadata") == []
    assert fake.subcmd_calls("add_custom_column") == []
    assert fake.subcmd_calls("custom_columns") == []


def test_sync_timestamp_conversion_lands_in_set_metadata():
    body = _book_body("user#A", md5="abc", last_access=1748000000000)
    store = _store_with_books([body])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=""),
        "add_custom_column": _cp(),
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),
            _cp(stdout="[]"),
        ],
        "set_metadata": _cp(),
    })
    c = _client(fake)
    sync_reading_state(store, c)
    set_calls = fake.subcmd_calls("set_metadata")
    joined = " ".join(set_calls[0])
    assert "#last_read:2025-05-23T11:33:20+00:00" in joined


def test_sync_unmatched_boox_not_synced():
    body = _book_body("user#A", md5=None, name="No MD5")
    store = _store_with_books([body])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=""),
        "add_custom_column": _cp(),
        "list": _cp(stdout="[]"),
    })
    c = _client(fake)
    summary = sync_reading_state(store, c)
    assert summary.updated == 0
    assert summary.unchanged == 0
    assert fake.subcmd_calls("set_metadata") == []


def test_sync_propagates_set_metadata_failure_into_errors():
    body = _book_body("user#A", md5="abc")
    store = _store_with_books([body])
    fake = _FakeCalibre({
        "custom_columns": _cp(stdout=""),
        "add_custom_column": _cp(),
        "list": [
            _cp(stdout=json.dumps([
                {"id": 7, "title": "X", "authors": "Y", "identifiers": {"md5": "abc"}}
            ])),
            _cp(stdout="[]"),
        ],
        "set_metadata": _cp(returncode=1, stderr="db locked"),
    })
    c = _client(fake)
    summary = sync_reading_state(store, c)
    assert summary.updated == 0
    assert len(summary.errors) == 1
    assert summary.errors[0]["calibre_book_id"] == 7


# --------------------------- live smoke ------------------------------------


@pytest.mark.live
def test_live_calibre_list_books_smoke():
    """Hit the real Calibre Content Server and pull the book list.

    Doesn't write anything; only verifies the URL+creds form goes
    through the gateway's auth layer. Skipped unless
    ``BOOX_RUN_LIVE_TESTS`` is set (handled by conftest).
    """
    if not os.environ.get("CALIBRE_USERNAME"):
        pytest.skip("CALIBRE_* env vars not present in this environment")
    client = CalibreClient()
    books = client.list_books()
    # Just assert the call succeeded; library can be empty.
    assert isinstance(books, list)
