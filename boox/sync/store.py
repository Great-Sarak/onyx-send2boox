"""Local SQLite mirror of synced channel docs.

#35 lands the persistence floor that the per-channel sync loops (#36
``NOTE_TREE``, #37 ``READER_LIBRARY``) write into. ``LocalStore`` is a
thin wrapper over ``sqlite3`` — four tables, a handful of methods, no
ORM. Schema kept lean because this is a local cache: the cloud is the
source of truth and the bodies are opaque JSON blobs we don't query
into.

Why SQLite (plan §Decisions #3)
-------------------------------

Fleet-of-one usage. SQLite gives us atomic writes, a single-file
database we can ship to a different host with one ``cp``, and is in the
stdlib. Postgres / KV stores would be overkill and add an ops surface.

Why this is not a BooxClient Pattern A subobject
------------------------------------------------

The other ``boox/*`` modules (``files``, ``subscriptions``,
``screensavers``, ``pushread``, ``sync._protocol``) all need a
``BooxClient`` back-reference to read ``client.session``,
``client.user_token``, ``client.sync_token``, etc. ``LocalStore``
doesn't touch the network and doesn't need anything off the client. It
attaches to nothing — callers instantiate it directly:

    from boox.sync import LocalStore
    with LocalStore() as store:
        store.upsert_doc("NOTE_TREE", doc_id, rev, body)

The sync-loop wiring in #36/#37 will pass a single shared ``LocalStore``
into each per-channel sync run, not hang one off the client.

WAL mode
--------

``PRAGMA journal_mode=WAL`` is set on every connect. WAL is a
database-wide property persisted in the file header, so the first
connection that flips it sets the mode for everyone else — subsequent
sets are idempotent no-ops. We do it on every open because we may be
the first connection after a fresh ``cp`` from another host, where the
journal mode reverts to the default ``delete``.

WAL is mandatory because the expected access pattern is one writer
(the sync loop) + many readers (skill scripts, ``boox sync status``
queries, debug shells). Without WAL, readers block the writer and
vice-versa, which deadlocks under realistic load.

For ``:memory:`` databases SQLite silently downgrades to the ``memory``
journal mode — that's fine, the test suite never needs cross-process
semantics from an in-memory DB.

Schema versioning
-----------------

A one-row ``schema_version`` table carries the current expected
version (``1`` as of #35). On open:

- Fresh DB → create all tables, insert ``schema_version=1``.
- Existing DB with matching version → continue.
- Existing DB with mismatched version → raise ``RuntimeError`` with
  instructions. No auto-migration in this issue; that's the migration
  story (#40+ if/when we need it).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union

from boox.errors import BooxError


SCHEMA_VERSION = 1

#: Default location for the sync DB. Honors ``BOOX_SYNC_DB`` env override.
DEFAULT_DB_PATH = "~/.cache/boox/sync.db"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS docs (
    channel TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    rev TEXT NOT NULL,
    body JSON NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (channel, doc_id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    channel TEXT PRIMARY KEY,
    last_seq TEXT NOT NULL,
    last_synced_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    channel TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    local_rev TEXT NOT NULL,
    remote_rev TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    noted_at INTEGER NOT NULL,
    PRIMARY KEY (channel, doc_id, local_rev, remote_rev)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


class LocalStoreError(BooxError):
    """Failure originating from the local SQLite store layer.

    Subclasses ``BooxError`` so callers that already catch the package
    base for cloud-side failures pick up store failures too. Currently
    raised only when ``__init__`` can't open the database file
    (permission denied, parent dir unwritable, etc.); the per-method
    ``sqlite3`` exceptions otherwise propagate unchanged so callers can
    distinguish ``OperationalError`` (locked / corruption) from
    ``IntegrityError`` (constraint violation) on their own terms.
    """


def _resolve_db_path(db_path: Optional[Union[str, Path]]) -> str:
    if db_path is None:
        env = os.environ.get("BOOX_SYNC_DB")
        db_path = env if env else DEFAULT_DB_PATH
    if isinstance(db_path, Path):
        return str(db_path)
    db_path = str(db_path)
    if db_path == ":memory:":
        return db_path
    return os.path.expanduser(db_path)


def _now_ns() -> int:
    return time.time_ns()


class LocalStore:
    """Thin SQLite mirror of synced channel docs.

    Parameters
    ----------
    db_path
        Path to the SQLite file. ``None`` → ``$BOOX_SYNC_DB`` if set,
        otherwise ``~/.cache/boox/sync.db``. ``":memory:"`` opens an
        in-memory DB (used by the unit tests). Parent directory is
        created if missing.

    Connection mode
    ---------------

    The connection runs in autocommit (``isolation_level=None``). Each
    method that mutates state issues exactly one statement which
    commits on its own. Wrap multiple operations in a ``with`` block to
    batch them into one transaction — the context manager runs
    ``BEGIN`` on entry, ``COMMIT`` on clean exit, ``ROLLBACK`` if the
    body raised.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        resolved = _resolve_db_path(db_path)
        if resolved != ":memory:":
            parent = os.path.dirname(resolved)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.db_path = resolved
        try:
            self._conn = sqlite3.connect(resolved, isolation_level=None)
        except sqlite3.OperationalError as e:
            raise LocalStoreError(
                f"failed to open sqlite db at {resolved!r}: {e}"
            ) from e
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._ensure_schema()

    # ---------- internals ----------

    def _configure_connection(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        )
        table_exists = cur.fetchone() is not None
        if not table_exists:
            cur.executescript(_SCHEMA_SQL)
            cur.execute(
                "INSERT INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            cur.close()
            return
        cur.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            cur.close()
            return
        version = row[0]
        cur.close()
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"LocalStore at {self.db_path!r} reports schema_version="
                f"{version}, this build expects {SCHEMA_VERSION}. No "
                f"auto-migration in #35: back up the DB, rerun against a "
                f"fresh path, or wait for the migration story (#40+)."
            )

    # ---------- docs ----------

    def upsert_doc(
        self,
        channel: str,
        doc_id: str,
        rev: str,
        body: Mapping[str, Any],
    ) -> None:
        """Insert or replace a doc row. ``body`` is serialized as JSON."""
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        self._conn.execute(
            "INSERT OR REPLACE INTO docs"
            "(channel, doc_id, rev, body, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, doc_id, rev, payload, _now_ns()),
        )

    def get_doc(self, channel: str, doc_id: str) -> Optional[dict]:
        """Return a dict with ``body`` parsed back to a dict, or ``None``."""
        row = self._conn.execute(
            "SELECT channel, doc_id, rev, body, updated_at FROM docs "
            "WHERE channel=? AND doc_id=?",
            (channel, doc_id),
        ).fetchone()
        if row is None:
            return None
        return _row_to_doc(row)

    def iter_channel(self, channel: str) -> Iterator[dict]:
        """Yield every doc in ``channel``, ordered by ``doc_id``.

        Lazy: the rows are streamed out of the cursor rather than read
        into a list, so iter-then-stop on a 100k-row channel doesn't
        allocate 100k dicts up front.
        """
        cur = self._conn.execute(
            "SELECT channel, doc_id, rev, body, updated_at FROM docs "
            "WHERE channel=? ORDER BY doc_id",
            (channel,),
        )
        try:
            for row in cur:
                yield _row_to_doc(row)
        finally:
            cur.close()

    # ---------- checkpoints ----------

    def get_checkpoint(self, channel: str) -> Optional[str]:
        """Return the last persisted ``since`` token, or ``None`` if
        this channel has never been synced (pre-first-sync sentinel)."""
        row = self._conn.execute(
            "SELECT last_seq FROM checkpoints WHERE channel=?",
            (channel,),
        ).fetchone()
        return row[0] if row is not None else None

    def set_checkpoint(self, channel: str, last_seq: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints"
            "(channel, last_seq, last_synced_at) "
            "VALUES (?, ?, ?)",
            (channel, last_seq, _now_ns()),
        )

    # ---------- conflicts ----------

    def log_conflict(
        self,
        channel: str,
        doc_id: str,
        local_rev: str,
        remote_rev: str,
    ) -> None:
        """Record a divergence. Idempotent: the PK is the full quadruple
        so re-logging the same conflict during a retry doesn't double up."""
        self._conn.execute(
            "INSERT OR IGNORE INTO conflicts"
            "(channel, doc_id, local_rev, remote_rev, resolved, noted_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (channel, doc_id, local_rev, remote_rev, _now_ns()),
        )

    def iter_conflicts(
        self, channel: str, resolved: bool = False
    ) -> Iterator[dict]:
        """Yield conflict rows for ``channel``. ``resolved=False`` (default)
        returns the open work queue for #40's resolver; ``resolved=True``
        returns the audit trail of already-closed conflicts."""
        cur = self._conn.execute(
            "SELECT channel, doc_id, local_rev, remote_rev, "
            "resolved, noted_at FROM conflicts "
            "WHERE channel=? AND resolved=? ORDER BY noted_at",
            (channel, 1 if resolved else 0),
        )
        try:
            for row in cur:
                yield dict(row)
        finally:
            cur.close()

    def mark_conflict_resolved(
        self,
        channel: str,
        doc_id: str,
        local_rev: str,
        remote_rev: str,
    ) -> None:
        self._conn.execute(
            "UPDATE conflicts SET resolved=1 "
            "WHERE channel=? AND doc_id=? "
            "AND local_rev=? AND remote_rev=?",
            (channel, doc_id, local_rev, remote_rev),
        )

    # ---------- lifecycle ----------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalStore":
        self._conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")


def _row_to_doc(row: sqlite3.Row) -> dict:
    return {
        "channel": row["channel"],
        "doc_id": row["doc_id"],
        "rev": row["rev"],
        "body": json.loads(row["body"]),
        "updated_at": row["updated_at"],
    }
