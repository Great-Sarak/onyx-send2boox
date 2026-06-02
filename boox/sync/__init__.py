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
from boox.sync.notes import (
    Note,
    NoteFolder,
    NoteOperation,
    NoteRecord,
    get_note,
    iter_notes,
    pull_notes,
)
from boox.sync.store import LocalStore, LocalStoreError

__all__ = [
    "ChangesResult",
    "LocalStore",
    "LocalStoreError",
    "Note",
    "NoteFolder",
    "NoteOperation",
    "NoteRecord",
    "SyncClient",
    "SyncProtocolError",
    "get_note",
    "iter_notes",
    "pull_notes",
]
