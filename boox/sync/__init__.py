"""Sync subpackage — Sync Gateway protocol primitives + local SQLite mirror.

Phase 4 splits the original ``boox/sync.py`` (#34) into a small package so
the new ``LocalStore`` (#35) lands beside the protocol code without a
460-line file growing further. Public surface unchanged:
``from boox.sync import SyncClient, ChangesResult, SyncProtocolError`` keeps
working because everything is re-exported here.

Modules
-------

- ``_protocol`` — HTTP primitives against ``/neocloud/*`` (the file that
  used to be ``boox/sync.py``). Underscore-prefixed because callers should
  import the classes from ``boox.sync`` directly, not reach into a private
  submodule.
- ``store`` — ``LocalStore``, a thin SQLite-backed mirror of synced channel
  docs (#35). Not a ``BooxClient`` Pattern A subobject: it's a standalone
  persistence helper with no client back-ref, so it doesn't attach to
  ``client.sync_store``. See ``flora/boox-plan-2026-05-31.md`` §Decisions.
"""

from __future__ import annotations

from boox.sync._protocol import ChangesResult, SyncClient, SyncProtocolError
from boox.sync.calibre_bridge import (
    BookMatch,
    CalibreBook,
    CalibreBridgeError,
    CalibreClient,
    MatchResult,
    SyncSummary,
    match_books,
    sync_reading_state,
)
from boox.sync.notes import (
    Note,
    NoteFolder,
    NoteOperation,
    NoteRecord,
    get_note,
    iter_notes,
    pull_notes,
)
from boox.sync.reader import (
    Book,
    BookBackend,
    BookProgress,
    Bookmark,
    LibraryRecord,
    ReaderNote,
    get_book,
    iter_book_progress_for_book,
    iter_books,
    iter_bookmarks_for_book,
    iter_reader_notes_for_book,
    pull_library,
)
from boox.sync.store import LocalStore, LocalStoreError

__all__ = [
    "Book",
    "BookBackend",
    "BookMatch",
    "BookProgress",
    "Bookmark",
    "CalibreBook",
    "CalibreBridgeError",
    "CalibreClient",
    "ChangesResult",
    "MatchResult",
    "SyncSummary",
    "match_books",
    "sync_reading_state",
    "LibraryRecord",
    "LocalStore",
    "LocalStoreError",
    "Note",
    "NoteFolder",
    "NoteOperation",
    "NoteRecord",
    "ReaderNote",
    "SyncClient",
    "SyncProtocolError",
    "get_book",
    "get_note",
    "iter_book_progress_for_book",
    "iter_books",
    "iter_bookmarks_for_book",
    "iter_notes",
    "iter_reader_notes_for_book",
    "pull_library",
    "pull_notes",
]
