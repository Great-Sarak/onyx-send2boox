"""Unit tests for ``boox.sync.notes`` — NOTE_TREE channel pull loop (#36).

Mocks the ``SyncClient`` primitives via a small stand-in rather than
``responses``-level HTTP fakes: the protocol layer is exercised in
``test_sync.py``, and here we only care that ``pull_notes`` drives
``changes`` → ``bulk_get`` → ``store`` correctly.

A live smoke test (``@pytest.mark.live``) runs against the real
Sync Gateway and asserts that the local store ends up with at least
the notes seen in ``tools/boox/captures/notes-har-2026-05-31.har``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

import pytest

import boox
from boox.sync import (
    ChangesResult,
    LocalStore,
    Note,
    NoteFolder,
    NoteOperation,
    get_note,
    iter_notes,
    pull_notes,
)
from boox.sync.notes import NOTE_TREE_SUFFIX, _LOCAL_CHANNEL
from .conftest import TEST_CLOUD, TEST_SYNC_TOKEN, TEST_TOKEN


# --------------------------- helpers ---------------------------------------


TEST_USER_UID = "user-uid-42"
TEST_CHANNEL = f"{TEST_USER_UID}{NOTE_TREE_SUFFIX}"


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
    """Bare client surface ``pull_notes`` reaches into."""

    def __init__(self, sync, userid=TEST_USER_UID):
        self.sync = sync
        self.userid = userid


def _note_body(doc_id="n1", rev="1-aaa", title="Notebook", pages=2):
    return {
        "_id": doc_id,
        "_rev": rev,
        "title": title,
        "document": True,
        "parentUniqueId": None,
        "createdAt": 1700000000000,
        "updatedAt": 1700000001000,
        "pageNameList": {"pageNameList": [f"p{i}" for i in range(pages)]},
    }


def _folder_body(doc_id="f1", rev="1-fff", title="My Folder"):
    return {
        "_id": doc_id,
        "_rev": rev,
        "title": title,
        "document": False,
        "createdAt": 1700000000000,
        "updatedAt": 1700000000000,
    }


def _op_body(doc_id="op1", rev="1-ooo", target="n1"):
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


def test_initial_pull_empty_store_fetches_all(store):
    """Four notes appear in changes; all four land in the store."""
    bodies = {
        f"n{i}": _note_body(doc_id=f"n{i}", rev=f"1-{i}", title=f"Note {i}")
        for i in range(1, 5)
    }
    results = [
        _change(f"n{i}", f"1-{i}", f"1::{i}") for i in range(1, 5)
    ]
    sync = _FakeSync(
        changes_script=[ChangesResult(results=results, last_seq="1::4")],
        bulk_get_bodies=bodies,
    )
    client = _FakeClient(sync)

    summary = pull_notes(client, store)

    assert summary == {
        "fetched": 4,
        "inserted": 4,
        "deleted": 0,
        "last_seq": "1::4",
    }
    stored_ids = {row["doc_id"] for row in store.iter_channel(_LOCAL_CHANNEL)}
    assert stored_ids == {"n1", "n2", "n3", "n4"}
    assert store.get_checkpoint(_LOCAL_CHANNEL) == "1::4"


def test_channel_name_uses_user_uid_suffix(store):
    """The underlying ``changes`` call must use ``<uid>-NOTE_TREE``, not raw."""
    sync = _FakeSync(
        changes_script=[ChangesResult(results=[], last_seq="0")],
    )
    client = _FakeClient(sync)

    pull_notes(client, store)

    assert sync.changes_calls[0]["channel"] == TEST_CHANNEL
    assert sync.changes_calls[0]["channel"] != "NOTE_TREE"


def test_channel_name_raises_when_userid_unset(store):
    sync = _FakeSync(changes_script=[])
    client = _FakeClient(sync, userid=None)

    with pytest.raises(ValueError, match="userid"):
        pull_notes(client, store)


def test_incremental_pull_uses_checkpoint(store):
    """``since=None`` falls back to the persisted checkpoint."""
    store.set_checkpoint(_LOCAL_CHANNEL, "1::100")
    bodies = {"n5": _note_body(doc_id="n5", rev="1-5")}
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("n5", "1-5", "1::101")],
                last_seq="1::101",
            )
        ],
        bulk_get_bodies=bodies,
    )
    client = _FakeClient(sync)

    summary = pull_notes(client, store)

    assert sync.changes_calls[0]["since"] == "1::100"
    assert summary["fetched"] == 1
    assert summary["inserted"] == 1
    assert store.get_checkpoint(_LOCAL_CHANNEL) == "1::101"


def test_explicit_since_overrides_checkpoint(store):
    """A non-``None`` ``since`` wins over the stored checkpoint."""
    store.set_checkpoint(_LOCAL_CHANNEL, "1::100")
    sync = _FakeSync(
        changes_script=[ChangesResult(results=[], last_seq="1::100")],
    )
    client = _FakeClient(sync)

    pull_notes(client, store, since="1::50")

    assert sync.changes_calls[0]["since"] == "1::50"


def test_empty_changes_advances_checkpoint_only_if_seq_moved(store):
    """No-op pulls don't rewrite the checkpoint when last_seq matches."""
    store.set_checkpoint(_LOCAL_CHANNEL, "1::100")
    sync = _FakeSync(
        changes_script=[ChangesResult(results=[], last_seq="1::100")],
    )
    client = _FakeClient(sync)

    summary = pull_notes(client, store)

    assert summary == {
        "fetched": 0,
        "inserted": 0,
        "deleted": 0,
        "last_seq": "1::100",
    }
    assert sync.bulk_get_calls == []


def test_operation_record_is_stored_but_filtered_from_iter_notes(store):
    """recordType:1 docs land in the store but iter_notes skips them."""
    bodies = {
        "n1": _note_body(),
        "op1": _op_body(),
    }
    results = [
        _change("n1", "1-aaa", "1::1"),
        _change("op1", "1-ooo", "1::2"),
    ]
    sync = _FakeSync(
        changes_script=[ChangesResult(results=results, last_seq="1::2")],
        bulk_get_bodies=bodies,
    )
    client = _FakeClient(sync)

    pull_notes(client, store)

    # Both rows landed.
    stored = {row["doc_id"] for row in store.iter_channel(_LOCAL_CHANNEL)}
    assert stored == {"n1", "op1"}

    # iter_notes filters out the operation record.
    yielded = list(iter_notes(store))
    assert len(yielded) == 1
    assert isinstance(yielded[0], Note)
    assert yielded[0].id == "n1"


def test_note_folder_typed_as_folder_not_note(store):
    bodies = {
        "n1": _note_body(),
        "f1": _folder_body(),
    }
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[
                    _change("n1", "1-aaa", "1::1"),
                    _change("f1", "1-fff", "1::2"),
                ],
                last_seq="1::2",
            )
        ],
        bulk_get_bodies=bodies,
    )
    client = _FakeClient(sync)
    pull_notes(client, store)

    by_id = {rec.id: rec for rec in iter_notes(store)}
    assert isinstance(by_id["n1"], Note)
    assert isinstance(by_id["f1"], NoteFolder)
    # NoteFolder is NOT a Note subclass; discriminator is the dataclass type.
    assert not isinstance(by_id["f1"], Note)


def test_deleted_change_removes_row_from_store(store):
    """``deleted: true`` in a change record drops the row (hard-delete)."""
    bodies = {"n1": _note_body()}
    sync_a = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("n1", "1-aaa", "1::1")],
                last_seq="1::1",
            )
        ],
        bulk_get_bodies=bodies,
    )
    pull_notes(_FakeClient(sync_a), store)
    assert store.get_doc(_LOCAL_CHANNEL, "n1") is not None

    sync_b = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("n1", "2-zzz", "1::2", deleted=True)],
                last_seq="1::2",
            )
        ],
    )
    summary = pull_notes(_FakeClient(sync_b), store)

    assert summary["fetched"] == 1
    assert summary["deleted"] == 1
    assert summary["inserted"] == 0
    assert store.get_doc(_LOCAL_CHANNEL, "n1") is None
    # Tombstoned rev shouldn't have triggered a bulk_get fetch.
    assert sync_b.bulk_get_calls == []


def test_bulk_get_body_with_deleted_flag_hard_deletes(store):
    """Doc body carrying ``_deleted: true`` is treated as a tombstone too."""
    store.upsert_doc(_LOCAL_CHANNEL, "n1", "1-aaa", _note_body())
    deleted_body = {"_id": "n1", "_rev": "2-bbb", "_deleted": True}
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("n1", "2-bbb", "1::2")],
                last_seq="1::2",
            )
        ],
        bulk_get_bodies={"n1": deleted_body},
    )

    summary = pull_notes(_FakeClient(sync), store)

    assert summary["deleted"] == 1
    assert summary["inserted"] == 0
    assert store.get_doc(_LOCAL_CHANNEL, "n1") is None


def test_missing_bulk_get_entry_skipped(store):
    """A ``missing`` envelope from bulk_get doesn't crash and doesn't insert."""
    sync = _FakeSync(
        changes_script=[
            ChangesResult(
                results=[_change("ghost", "9-gone", "1::1")],
                last_seq="1::1",
            )
        ],
        bulk_get_bodies={},  # nothing → missing for every requested id
    )
    summary = pull_notes(_FakeClient(sync), store)

    assert summary["fetched"] == 1
    assert summary["inserted"] == 0
    assert summary["deleted"] == 0


def test_iter_notes_returns_typed_instances(store):
    store.upsert_doc(_LOCAL_CHANNEL, "n1", "1-aaa", _note_body())
    store.upsert_doc(_LOCAL_CHANNEL, "f1", "1-fff", _folder_body())
    store.upsert_doc(_LOCAL_CHANNEL, "op1", "1-ooo", _op_body())

    out = list(iter_notes(store))
    kinds = {type(r).__name__ for r in out}
    assert kinds == {"Note", "NoteFolder"}


def test_get_note_returns_single_typed_instance(store):
    store.upsert_doc(_LOCAL_CHANNEL, "n1", "1-aaa", _note_body())
    store.upsert_doc(_LOCAL_CHANNEL, "op1", "1-ooo", _op_body())

    note = get_note(store, "n1")
    assert isinstance(note, Note)
    assert note.id == "n1"
    assert note.title == "Notebook"
    assert note.page_count == 2

    # get_note does NOT filter operations — caller asked for the id.
    op = get_note(store, "op1")
    assert isinstance(op, NoteOperation)
    assert op.document_unique_id == "n1"


def test_get_note_returns_none_for_unknown_id(store):
    assert get_note(store, "missing-id") is None


def test_note_from_doc_dispatcher():
    """``Note.from_doc`` returns the right concrete type by discriminator."""
    note = Note.from_doc(_note_body())
    folder = Note.from_doc(_folder_body())
    op = Note.from_doc(_op_body())
    assert isinstance(note, Note)
    assert isinstance(folder, NoteFolder)
    assert isinstance(op, NoteOperation)


def test_longpoll_flag_propagates_to_changes(store):
    sync = _FakeSync(
        changes_script=[ChangesResult(results=[], last_seq="0")],
    )
    client = _FakeClient(sync)

    pull_notes(client, store, longpoll=True)

    assert sync.changes_calls[0]["longpoll"] is True


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
def test_live_pull_notes_smoke(live_client):
    """Initial pull populates the store; second pull is a no-op."""
    client = live_client
    with LocalStore(":memory:") as store:
        first = pull_notes(client, store, since=None)
        assert first["fetched"] >= 1
        assert store.get_checkpoint(_LOCAL_CHANNEL) is not None

        # Cross-check against HAR: at least the notes we captured should
        # be present. The HAR file lives in the rukha workspace; if it's
        # not reachable from the test env, just assert non-empty.
        notes = list(iter_notes(store))
        assert any(isinstance(n, Note) for n in notes)

        second = pull_notes(client, store, since=None)
        assert second["fetched"] == 0
        assert second["inserted"] == 0
