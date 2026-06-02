"""Unit tests for ``boox.sync.store.LocalStore`` (#35).

In-memory SQLite for all unit tests except the concurrent-access test,
which needs a real file because ``:memory:`` databases are
connection-scoped (a second ``sqlite3.connect(":memory:")`` opens a new
empty DB).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import types
from pathlib import Path

import pytest

from boox.errors import BooxError
from boox.sync import LocalStore, LocalStoreError
from boox.sync.store import DEFAULT_DB_PATH, SCHEMA_VERSION


# --------------------------- helpers ---------------------------------------


@pytest.fixture
def store():
    s = LocalStore(":memory:")
    try:
        yield s
    finally:
        s.close()


SAMPLE_BODY = {
    "_id": "doc-1",
    "title": "Notes",
    "tags": ["a", "b"],
    "meta": {"pinned": True, "count": 3},
}


# --------------------------- schema + pragmas ------------------------------


def test_localstoreerror_subclasses_booxerror():
    assert issubclass(LocalStoreError, BooxError)


def test_default_db_path_constant():
    assert DEFAULT_DB_PATH == "~/.cache/boox/sync.db"


def test_pragmas_enabled(store):
    fk = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    # :memory: silently downgrades WAL to "memory" — both are acceptable
    # markers that we asked for WAL on connect.
    assert mode.lower() in {"wal", "memory"}


def test_schema_tables_created(store):
    names = {
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"docs", "checkpoints", "conflicts", "schema_version"} <= names


def test_schema_version_seeded(store):
    row = store._conn.execute(
        "SELECT version FROM schema_version"
    ).fetchone()
    assert row[0] == SCHEMA_VERSION


# --------------------------- docs ------------------------------------------


def test_upsert_and_get_roundtrips_json(store):
    store.upsert_doc("NOTE_TREE", "doc-1", "1-abc", SAMPLE_BODY)
    row = store.get_doc("NOTE_TREE", "doc-1")
    assert row is not None
    assert row["channel"] == "NOTE_TREE"
    assert row["doc_id"] == "doc-1"
    assert row["rev"] == "1-abc"
    assert row["body"] == SAMPLE_BODY
    assert isinstance(row["updated_at"], int)


def test_get_doc_missing_returns_none(store):
    assert store.get_doc("NOTE_TREE", "nope") is None


def test_upsert_overwrites_with_latest_rev(store):
    store.upsert_doc("NOTE_TREE", "doc-1", "1-abc", {"v": 1})
    first = store.get_doc("NOTE_TREE", "doc-1")
    # Ensure the clock advances enough for time_ns() to differ even on
    # platforms with coarse monotonic resolution.
    time.sleep(0.001)
    store.upsert_doc("NOTE_TREE", "doc-1", "2-def", {"v": 2})
    second = store.get_doc("NOTE_TREE", "doc-1")
    assert second["rev"] == "2-def"
    assert second["body"] == {"v": 2}
    assert second["updated_at"] >= first["updated_at"]


def test_iter_channel_is_generator(store):
    store.upsert_doc("NOTE_TREE", "doc-1", "1-a", {"v": 1})
    it = store.iter_channel("NOTE_TREE")
    assert isinstance(it, types.GeneratorType)
    items = list(it)
    assert len(items) == 1


def test_iter_channel_yields_all_docs(store):
    for i in range(100):
        store.upsert_doc(
            "READER_LIBRARY", f"doc-{i:03d}", "1-x", {"i": i}
        )
    rows = list(store.iter_channel("READER_LIBRARY"))
    assert len(rows) == 100
    # Ordered by doc_id, so the first one is doc-000.
    assert rows[0]["doc_id"] == "doc-000"
    assert rows[-1]["doc_id"] == "doc-099"


def test_iter_channel_scoped_to_channel(store):
    store.upsert_doc("NOTE_TREE", "n1", "1-a", {})
    store.upsert_doc("READER_LIBRARY", "r1", "1-a", {})
    assert [r["doc_id"] for r in store.iter_channel("NOTE_TREE")] == ["n1"]
    assert [r["doc_id"] for r in store.iter_channel("READER_LIBRARY")] == ["r1"]


# --------------------------- checkpoints -----------------------------------


def test_get_checkpoint_unknown_channel_returns_none(store):
    assert store.get_checkpoint("NOTE_TREE") is None


def test_checkpoint_roundtrip(store):
    store.set_checkpoint("NOTE_TREE", "seq-1")
    assert store.get_checkpoint("NOTE_TREE") == "seq-1"
    store.set_checkpoint("NOTE_TREE", "seq-2")
    assert store.get_checkpoint("NOTE_TREE") == "seq-2"


def test_checkpoints_scoped_to_channel(store):
    store.set_checkpoint("NOTE_TREE", "n-1")
    store.set_checkpoint("READER_LIBRARY", "r-1")
    assert store.get_checkpoint("NOTE_TREE") == "n-1"
    assert store.get_checkpoint("READER_LIBRARY") == "r-1"


# --------------------------- conflicts -------------------------------------


def test_log_and_iter_conflict(store):
    store.log_conflict("NOTE_TREE", "doc-1", "1-local", "1-remote")
    rows = list(store.iter_conflicts("NOTE_TREE"))
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "doc-1"
    assert rows[0]["local_rev"] == "1-local"
    assert rows[0]["remote_rev"] == "1-remote"
    assert rows[0]["resolved"] == 0


def test_log_conflict_is_idempotent(store):
    store.log_conflict("NOTE_TREE", "doc-1", "1-l", "1-r")
    store.log_conflict("NOTE_TREE", "doc-1", "1-l", "1-r")
    assert len(list(store.iter_conflicts("NOTE_TREE"))) == 1


def test_mark_conflict_resolved_filters_iter(store):
    store.log_conflict("NOTE_TREE", "d1", "1-l", "1-r")
    store.log_conflict("NOTE_TREE", "d2", "1-l", "1-r")
    store.mark_conflict_resolved("NOTE_TREE", "d1", "1-l", "1-r")
    open_rows = list(store.iter_conflicts("NOTE_TREE", resolved=False))
    closed_rows = list(store.iter_conflicts("NOTE_TREE", resolved=True))
    assert [r["doc_id"] for r in open_rows] == ["d2"]
    assert [r["doc_id"] for r in closed_rows] == ["d1"]


# --------------------------- context manager -------------------------------


def test_context_manager_commits_on_success(tmp_path):
    db = tmp_path / "sync.db"
    with LocalStore(str(db)) as s:
        s.upsert_doc("NOTE_TREE", "doc-1", "1-a", {"v": 1})
        s.set_checkpoint("NOTE_TREE", "seq-1")
    # Reopen and confirm the writes survived.
    s2 = LocalStore(str(db))
    try:
        assert s2.get_doc("NOTE_TREE", "doc-1")["body"] == {"v": 1}
        assert s2.get_checkpoint("NOTE_TREE") == "seq-1"
    finally:
        s2.close()


def test_context_manager_rolls_back_on_exception(tmp_path):
    db = tmp_path / "sync.db"
    s = LocalStore(str(db))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with s:
                s.upsert_doc("NOTE_TREE", "doc-1", "1-a", {"v": 1})
                raise RuntimeError("boom")
        assert s.get_doc("NOTE_TREE", "doc-1") is None
    finally:
        s.close()


# --------------------------- schema version mismatch -----------------------


def test_schema_version_mismatch_raises(tmp_path):
    db = tmp_path / "sync.db"
    s = LocalStore(str(db))
    s.close()
    # Hand-edit the version to something this build doesn't recognize.
    raw = sqlite3.connect(str(db))
    raw.execute("UPDATE schema_version SET version=999")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="schema_version=999"):
        LocalStore(str(db))


# --------------------------- WAL concurrency -------------------------------


def test_concurrent_reader_and_writer_under_wal(tmp_path):
    """Writer and reader on the same on-disk DB don't deadlock.

    With WAL enabled, a reader sees a consistent snapshot while a writer
    is mid-transaction. We assert both complete and the reader sees
    either the pre- or post-write state (both are valid under snapshot
    isolation).
    """
    db = tmp_path / "sync.db"
    # Seed via a short-lived instance so each thread can confirm the
    # row exists from a snapshot the moment it opens its own connection.
    seed = LocalStore(str(db))
    seed.upsert_doc("NOTE_TREE", "doc-1", "1-a", {"v": 1})
    seed.close()

    errors = []
    seen = []

    def do_reads():
        try:
            reader = LocalStore(str(db))
            for _ in range(50):
                row = reader.get_doc("NOTE_TREE", "doc-1")
                if row is not None:
                    seen.append(row["body"])
            reader.close()
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def do_writes():
        try:
            writer = LocalStore(str(db))
            for i in range(2, 52):
                writer.upsert_doc(
                    "NOTE_TREE", "doc-1", f"{i}-x", {"v": i}
                )
            writer.close()
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    t_r = threading.Thread(target=do_reads)
    t_w = threading.Thread(target=do_writes)
    t_r.start()
    t_w.start()
    t_r.join(timeout=10)
    t_w.join(timeout=10)
    assert not t_r.is_alive()
    assert not t_w.is_alive()
    assert errors == []
    assert seen, "reader should have observed at least one snapshot"

    final = LocalStore(str(db))
    try:
        assert final.get_doc("NOTE_TREE", "doc-1")["body"]["v"] == 51
    finally:
        final.close()


# --------------------------- env override + defaults -----------------------


def test_env_override_used_when_db_path_none(tmp_path, monkeypatch):
    db = tmp_path / "from_env.db"
    monkeypatch.setenv("BOOX_SYNC_DB", str(db))
    s = LocalStore()
    try:
        assert s.db_path == str(db)
        assert db.exists()
    finally:
        s.close()


def test_parent_directory_created(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "sync.db"
    s = LocalStore(str(nested))
    try:
        assert nested.exists()
    finally:
        s.close()
