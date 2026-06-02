"""Unit tests for ``boox.sync.reader`` — READER_LIBRARY channel pull loop (#37).

Mirrors the ``test_sync_notes.py`` pattern: stand-in for ``client.sync``
that captures calls and replays scripted ``ChangesResult`` / ``bulk_get``
responses. The protocol layer is exercised in ``test_sync.py``; here we
only care that ``pull_library`` drives ``changes`` → ``bulk_get`` →
``store`` correctly and that the typed shapes / lazy-decode behaviour
match the brief.

Live smoke runs against the real Sync Gateway (``BOOX_RUN_LIVE_TESTS``)
and asserts the user's actual library lands with at least one book whose
``backend.md5`` parses as a 32-char hex string.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Mapping, Optional, Sequence

import pytest

import boox
from boox.sync import (
    Book,
    BookBackend,
    ChangesResult,
    LibraryOperation,
    LocalStore,
    ReaderNote,
    get_book,
    iter_books,
    iter_reader_notes_for_book,
    pull_library,
)
from boox.sync.reader import READER_LIBRARY_SUFFIX, _LOCAL_CHANNEL


# --------------------------- helpers ---------------------------------------


TEST_USER_UID = "user-uid-42"
TEST_CHANNEL = f"{TEST_USER_UID}{READER_LIBRARY_SUFFIX}"


class _FakeSync:
    """Stand-in for ``client.sync`` capturing calls and replaying scripts."""

    def __init__(
        self,
        changes_script: Sequence[ChangesResult],
        bulk_get_bodies: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        self._changes_script = list(changes_script)
        self._bulk_get_bodies = dict(bulk_get_bodies or {})
        self.changes_calls: List[dict] = []
        self.bulk_get_calls: List[List[Mapping[str, str]]] = []

    def changes(self, channel, since=None, longpoll=False, **kwargs):
        self.changes_calls.append(
            {"channel": channel, "since": since, "longpoll": longpoll}
        )
        if not self._changes_script:
            return ChangesResult(results=[], last_seq=since)
        return self._changes_script.pop(0)

    def bulk_get(self, doc_revs):
        self.bulk_get_calls.append(list(doc_revs))
        out: List[Mapping[str, Any]] = []
        for dr in doc_revs:
            body = self._bulk_get_bodies.get(dr["id"])
            if body is None:
                out.append({"missing": dr["rev"], "id": dr["id"]})
            else:
                out.append({"ok": body})
        return out


class _FakeClient:
    def __init__(self, sync, userid=TEST_USER_UID):
        self.sync = sync
        self.userid = userid


def _backend_blob(
    md5="8f82867a2a6807c462ffa75017798cbe",
    page=r'{"pageReferenceId":"pdf-4-0"}',
    total_page=3,
):
    """Build an extraAttributes JSON string matching the 2026-05-31 HAR shape."""
    return json.dumps(
        {
            "backend": {
                "md5": md5,
                "current_page_position_v2": page,
                "total_page": total_page,
                "document_category": "NORMAL",
                "layout_type": "singlePage",
                "orientation": 1,
                "actual_scale": 3.084577,
                "viewport": r'{"bottom":2417.6}',
                "doc_id": "doc-uuid",
            },
            "dummyObject": False,
        }
    )


def _book_body(
    doc_id="user-uid-42#book1",
    rev="1-aaa",
    name="Pregens-Samo-Lvl1.pdf",
    extra=None,
):
    body = {
        "_id": doc_id,
        "_rev": rev,
        "UUID": doc_id.split("#")[-1],
        "name": name,
        "lastAccess": 1740367277657,
        "lastModified": 1740367277657,
        "progress": 0.5,
        "readingStatus": 1,
        "rating": 0,
        "favorite": 0,
        "location": "/storage/77A3-F8F2/Books/Paizo/Level 1/",
        "nativeAbsolutePath": "/storage/77A3-F8F2/Books/Paizo/Level 1/Pregens-Samo-Lvl1.pdf",
        "storageId": "storage-id",
        "size": 604427,
        "idString": "id-str",
        "hashTag": "8f82867a2a6807c462ffa75017798cbe",
        "fileSyncStatus": 0,
        "userDataSyncStatus": 0,
    }
    body["extraAttributes"] = _backend_blob() if extra is None else extra
    return body


def _reader_note_body(
    doc_id="user-uid-42#note1",
    rev="1-nnn",
    document_id="user-uid-42#book1",
    commit_info=None,
):
    body = {
        "_id": doc_id,
        "_rev": rev,
        "UUID": doc_id.split("#")[-1],
        "documentId": document_id,
        "title": "annotations",
        "currentShapeType": 0,
        "background": None,
        "strokeColor": -16777216,
        "strokeWidth": 3.0,
        "readerNotePageNameMap": {},
        "createdAt": 1740358887604,
        "updatedAt": 1740367277657,
    }
    if commit_info is None:
        body["commitInfo"] = json.dumps({"commitId": "c1", "pages": 4})
    else:
        body["commitInfo"] = commit_info
    return body


def _op_body(doc_id="op1", rev="1-ooo", target="user-uid-42#book1"):
    return {
        "_id": doc_id,
        "_rev": rev,
        "recordType": 1,
        "commitType": 1,
        "commitStatus": 1,
        "documentUniqueId": target,
    }


def _change(doc_id, rev, seq, deleted=False):
    rec = {
        "seq": seq,
        "id": doc_id,
        "changes": [{"rev": rev}],
    }
    if deleted:
        rec["deleted"] = True
    return rec


@pytest.fixture
def store():
    s = LocalStore(":memory:")
    try:
        yield s
    finally:
        s.close()


# --------------------------- pull flow -------------------------------------


def test_initial_pull_mixed_doc_kinds(store):
    """Two books + one reader-note + one op land typed correctly."""
    bodies = {
        "user-uid-42#book1": _book_body(doc_id="user-uid-42#book1", rev="1-a"),
        "user-uid-42#book2": _book_body(doc_id="user-uid-42#book2", rev="1-b", name="other.epub"),
        "user-uid-42#note1": _reader_note_body(),
        "op1": _op_body(),
    }
    results = [
        _change("user-uid-42#book1", "1-a", "1::1"),
        _change("user-uid-42#book2", "1-b", "1::2"),
        _change("user-uid-42#note1", "1-nnn", "1::3"),
        _change("op1", "1-ooo", "1::4"),
    ]
    sync = _FakeSync(
        changes_script=[ChangesResult(results=results, last_seq="1::4")],
        bulk_get_bodies=bodies,
    )

    summary = pull_library(_FakeClient(sync), store)

    assert summary == {
        "fetched": 4,
        "inserted": 4,
        "deleted": 0,
        "last_seq": "1::4",
    }
    stored = {row["doc_id"] for row in store.iter_channel(_LOCAL_CHANNEL)}
    assert stored == {
        "user-uid-42#book1",
        "user-uid-42#book2",
        "user-uid-42#note1",
        "op1",
    }


def test_channel_name_uses_user_uid_suffix(store):
    sync = _FakeSync(changes_script=[ChangesResult(results=[], last_seq="0")])

    pull_library(_FakeClient(sync), store)

    assert sync.changes_calls[0]["channel"] == TEST_CHANNEL
    assert sync.changes_calls[0]["channel"] != "READER_LIBRARY"


def test_channel_name_raises_when_userid_unset(store):
    sync = _FakeSync(changes_script=[])
    with pytest.raises(ValueError, match="userid"):
        pull_library(_FakeClient(sync, userid=None), store)


def test_incremental_pull_uses_checkpoint(store):
    store.set_checkpoint(_LOCAL_CHANNEL, "1::100")
    bodies = {"user-uid-42#book9": _book_body(doc_id="user-uid-42#book9", rev="1-9")}
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("user-uid-42#book9", "1-9", "1::101")],
                last_seq="1::101",
            )
        ],
        bulk_get_bodies=bodies,
    )

    summary = pull_library(_FakeClient(sync), store)

    assert sync.changes_calls[0]["since"] == "1::100"
    assert summary["inserted"] == 1
    assert store.get_checkpoint(_LOCAL_CHANNEL) == "1::101"


def test_empty_changes_no_op(store):
    store.set_checkpoint(_LOCAL_CHANNEL, "1::100")
    sync = _FakeSync(
        changes_script=[ChangesResult(results=[], last_seq="1::100")],
    )

    summary = pull_library(_FakeClient(sync), store)

    assert summary == {
        "fetched": 0,
        "inserted": 0,
        "deleted": 0,
        "last_seq": "1::100",
    }
    assert sync.bulk_get_calls == []


def test_longpoll_flag_propagates(store):
    sync = _FakeSync(
        changes_script=[ChangesResult(results=[], last_seq="0")],
    )
    pull_library(_FakeClient(sync), store, longpoll=True)
    assert sync.changes_calls[0]["longpoll"] is True


def test_deleted_change_hard_deletes(store):
    """``deleted: true`` in a change record drops the row (hard-delete, mirrors #36)."""
    bodies = {"user-uid-42#book1": _book_body()}
    pull_library(
        _FakeClient(
            _FakeSync(
                changes_script=[
                    ChangesResult(
                        results=[_change("user-uid-42#book1", "1-aaa", "1::1")],
                        last_seq="1::1",
                    )
                ],
                bulk_get_bodies=bodies,
            )
        ),
        store,
    )
    assert store.get_doc(_LOCAL_CHANNEL, "user-uid-42#book1") is not None

    sync_b = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("user-uid-42#book1", "2-zzz", "1::2", deleted=True)],
                last_seq="1::2",
            )
        ],
    )
    summary = pull_library(_FakeClient(sync_b), store)

    assert summary["deleted"] == 1
    assert summary["inserted"] == 0
    assert store.get_doc(_LOCAL_CHANNEL, "user-uid-42#book1") is None
    assert sync_b.bulk_get_calls == []


def test_bulk_get_body_with_deleted_flag_hard_deletes(store):
    store.upsert_doc(_LOCAL_CHANNEL, "user-uid-42#book1", "1-aaa", _book_body())
    deleted_body = {"_id": "user-uid-42#book1", "_rev": "2-bbb", "_deleted": True}
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("user-uid-42#book1", "2-bbb", "1::2")],
                last_seq="1::2",
            )
        ],
        bulk_get_bodies={"user-uid-42#book1": deleted_body},
    )

    summary = pull_library(_FakeClient(sync), store)

    assert summary["deleted"] == 1
    assert summary["inserted"] == 0
    assert store.get_doc(_LOCAL_CHANNEL, "user-uid-42#book1") is None


def test_missing_bulk_get_entry_skipped(store):
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("ghost", "9-gone", "1::1")],
                last_seq="1::1",
            )
        ],
        bulk_get_bodies={},
    )
    summary = pull_library(_FakeClient(sync), store)

    assert summary["fetched"] == 1
    assert summary["inserted"] == 0
    assert summary["deleted"] == 0


# --------------------------- typed dispatch --------------------------------


def test_book_from_doc_dispatch():
    assert isinstance(Book.from_doc(_book_body()), Book)
    assert isinstance(Book.from_doc(_reader_note_body()), ReaderNote)
    assert isinstance(Book.from_doc(_op_body()), LibraryOperation)


def test_book_top_level_fields_parsed():
    book = Book.from_doc(_book_body())
    assert isinstance(book, Book)
    assert book.id == "user-uid-42#book1"
    assert book.uuid == "book1"
    assert book.name == "Pregens-Samo-Lvl1.pdf"
    assert book.location.startswith("/storage/")
    assert book.native_absolute_path.endswith("Pregens-Samo-Lvl1.pdf")
    assert book.size == 604427
    assert book.hash_tag == "8f82867a2a6807c462ffa75017798cbe"


def test_extra_attributes_lazy_decoded():
    book = Book.from_doc(_book_body())
    backend = book.backend
    assert isinstance(backend, BookBackend)
    assert backend.md5 == "8f82867a2a6807c462ffa75017798cbe"
    assert backend.current_page_position_v2 == r'{"pageReferenceId":"pdf-4-0"}'
    assert backend.total_page == 3
    assert backend.layout_type == "singlePage"
    assert backend.document_category == "NORMAL"
    # Re-access uses cache (same object identity).
    assert book.backend is backend


def test_extra_attributes_malformed_returns_none_and_warns(caplog):
    book = Book.from_doc(_book_body(extra="not json"))
    with caplog.at_level(logging.WARNING, logger="boox.sync.reader"):
        assert book.backend is None
    assert any("malformed extraAttributes" in r.message for r in caplog.records)


def test_extra_attributes_empty_returns_none():
    assert Book.from_doc(_book_body(extra="")).backend is None


def test_extra_attributes_missing_returns_none():
    body = _book_body()
    del body["extraAttributes"]
    assert Book.from_doc(body).backend is None


def test_extra_attributes_backend_not_a_mapping_returns_none():
    """If ``backend`` decodes to a non-mapping, surface None — don't crash."""
    body = _book_body(extra=json.dumps({"backend": "not-a-dict"}))
    assert Book.from_doc(body).backend is None


def test_reader_note_commit_info_lazy_decoded():
    rn = Book.from_doc(_reader_note_body())
    assert isinstance(rn, ReaderNote)
    assert rn.document_id == "user-uid-42#book1"
    decoded = rn.commit_info
    assert decoded == {"commitId": "c1", "pages": 4}
    # Cached.
    assert rn.commit_info is decoded


def test_reader_note_commit_info_malformed_returns_none(caplog):
    rn = Book.from_doc(_reader_note_body(commit_info="garbage"))
    with caplog.at_level(logging.WARNING, logger="boox.sync.reader"):
        assert rn.commit_info is None
    assert any("malformed commitInfo" in r.message for r in caplog.records)


def test_reader_note_commit_info_missing_returns_none():
    body = _reader_note_body()
    del body["commitInfo"]
    assert Book.from_doc(body).commit_info is None


# --------------------------- typed queries ---------------------------------


def test_iter_books_filters_to_books(store):
    store.upsert_doc(_LOCAL_CHANNEL, "user-uid-42#book1", "1-a", _book_body())
    store.upsert_doc(_LOCAL_CHANNEL, "user-uid-42#note1", "1-n", _reader_note_body())
    store.upsert_doc(_LOCAL_CHANNEL, "op1", "1-o", _op_body())

    out = list(iter_books(store))
    assert len(out) == 1
    assert isinstance(out[0], Book)
    assert out[0].id == "user-uid-42#book1"


def test_get_book_returns_book(store):
    store.upsert_doc(_LOCAL_CHANNEL, "user-uid-42#book1", "1-a", _book_body())
    b = get_book(store, "user-uid-42#book1")
    assert isinstance(b, Book)
    assert b.name == "Pregens-Samo-Lvl1.pdf"


def test_get_book_returns_none_for_non_book_rows(store):
    """A reader-note row under that id should not be returned as a Book."""
    store.upsert_doc(_LOCAL_CHANNEL, "user-uid-42#note1", "1-n", _reader_note_body())
    assert get_book(store, "user-uid-42#note1") is None


def test_get_book_returns_none_for_unknown_id(store):
    assert get_book(store, "user-uid-42#nope") is None


def test_iter_reader_notes_for_book_filters_by_document_id(store):
    store.upsert_doc(
        _LOCAL_CHANNEL,
        "user-uid-42#note1",
        "1-n",
        _reader_note_body(doc_id="user-uid-42#note1", document_id="user-uid-42#book1"),
    )
    store.upsert_doc(
        _LOCAL_CHANNEL,
        "user-uid-42#note2",
        "1-m",
        _reader_note_body(doc_id="user-uid-42#note2", document_id="user-uid-42#book2"),
    )
    store.upsert_doc(_LOCAL_CHANNEL, "user-uid-42#book1", "1-a", _book_body())

    out = list(iter_reader_notes_for_book(store, "user-uid-42#book1"))
    assert len(out) == 1
    assert isinstance(out[0], ReaderNote)
    assert out[0].id == "user-uid-42#note1"

    empty = list(iter_reader_notes_for_book(store, "user-uid-42#nobook"))
    assert empty == []


# --------------------------- live smoke ------------------------------------


@pytest.fixture
def live_client(live_token, live_sync_token):
    config = {
        "default": {
            "cloud": "push.boox.com",
            "token": live_token,
            "sync_token": live_sync_token,
        }
    }
    return boox.Boox(config)


@pytest.mark.live
def test_live_pull_library_smoke(live_client):
    """Pull populates store; at least one book has a 32-char hex md5."""
    client = live_client
    with LocalStore(":memory:") as store:
        first = pull_library(client, store, since=None)
        assert first["fetched"] >= 1
        assert store.get_checkpoint(_LOCAL_CHANNEL) is not None

        books = list(iter_books(store))
        assert len(books) >= 1

        # At least one book has a parseable md5 hex.
        md5_re = re.compile(r"[0-9a-f]{32}")
        md5s = [b.backend.md5 for b in books if b.backend and b.backend.md5]
        assert any(md5_re.fullmatch(m) for m in md5s), \
            f"no book carried a 32-char hex md5; saw {md5s!r}"

        # At least one book has a non-empty current_page_position_v2.
        positions = [
            b.backend.current_page_position_v2
            for b in books
            if b.backend and b.backend.current_page_position_v2
        ]
        assert any(positions), "no book carried current_page_position_v2"

        second = pull_library(client, store, since=None)
        assert second["fetched"] == 0
        assert second["inserted"] == 0
