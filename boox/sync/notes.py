"""NOTE_TREE channel sync — pull note metadata into the local store.

#36 lands the first per-channel sync loop on top of the protocol
primitives from #34 (``SyncClient``) and the persistence floor from
#35 (``LocalStore``). After this module runs, every NOTE_TREE doc the
Sync Gateway can see for ``client.userid`` is mirrored locally and
queryable through ``iter_notes`` / ``get_note``.

This is metadata-only sync. The per-page ink stroke binaries live at
``/<user_uid>/note/<note_id>/point/...`` and are out of scope here;
#38 handles binary fetch.

Channel name
------------

``<user_uid>-NOTE_TREE`` — assembled inside :func:`pull_notes` from
``client.userid`` (the field Phase 1 #27 sets after ``users/me``).
Callers pass the client, not the channel string, so the channel
naming convention stays in one place.

Doc kinds (HAR-confirmed against ``notes-har-2026-05-31.har``)
--------------------------------------------------------------

NOTE_TREE carries three discriminable kinds, dispatched by
:meth:`NoteRecord.from_doc`:

- ``document: true`` and no ``recordType`` → :class:`Note`. A real
  note (notebook or single page). Top-level fields per
  ``flora/BOOX.md`` §"Note metadata schema".
- ``document: false`` and no ``recordType`` → :class:`NoteFolder`.
  Folder grouping. Same shape as :class:`Note` but distinguished by
  the ``document`` flag so callers can filter.
- ``recordType: 1`` → :class:`NoteOperation`. Sync bookkeeping
  (delete / share operations against a note). Stored for completeness;
  :func:`iter_notes` filters these out so callers asking "what notes
  do I have" don't get bookkeeping records mixed in.

Implementation chooses **dataclasses** over TypedDict because we want
the discriminator dispatch to be a real classmethod (`from_doc`) and
``isinstance(x, Note)`` to work at the type level. TypedDict gives
structural shape but no runtime type — and the union-discriminator is
the load-bearing part of the public surface here.

Hard-delete policy (plan §Decisions, surfaced in PR body)
---------------------------------------------------------

When ``_changes`` returns a change record with ``deleted: true`` (or
the doc body carries ``_deleted: true``), we **remove the row from
``store``** rather than soft-delete. Rationale: this is a metadata
mirror of current cloud state, not a backup. If the cloud says the
doc is gone, the local mirror reflects that. Replay from tombstones
isn't a use case we support — anyone who needs history should pull
the channel again from ``since=None``.

A hard-deleted row in ``LocalStore`` simply means ``get_doc`` returns
``None`` and ``iter_channel`` skips it. The deletion count surfaces
in :func:`pull_notes`'s return summary so callers can log it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union

from boox.sync._protocol import SyncClient
from boox.sync.store import LocalStore


__all__ = [
    "Note",
    "NoteFolder",
    "NoteOperation",
    "NoteRecord",
    "NOTE_TREE_SUFFIX",
    "pull_notes",
    "iter_notes",
    "get_note",
]


NOTE_TREE_SUFFIX = "-NOTE_TREE"

#: Channel-table key under which NOTE_TREE rows live in ``LocalStore``.
#: We strip the ``<user_uid>-`` prefix so the same SQLite database
#: serves any account without rewriting on user switch — the cloud
#: channel name carries the uid; the local channel name is just the
#: kind.
_LOCAL_CHANNEL = "NOTE_TREE"


# --------------------------- typed shapes ----------------------------------


@dataclass
class _BaseNoteDoc:
    """Common fields shared by :class:`Note` and :class:`NoteFolder`."""

    id: str
    rev: Optional[str]
    title: Optional[str]
    parent_unique_id: Optional[str]
    created_at: Optional[int]
    updated_at: Optional[int]
    raw: Mapping[str, Any] = field(repr=False)


@dataclass
class Note(_BaseNoteDoc):
    """A NOTE_TREE doc with ``document: true``."""

    page_count: Optional[int] = None


@dataclass
class NoteFolder(_BaseNoteDoc):
    """A NOTE_TREE doc with ``document: false`` — a folder grouping."""


@dataclass
class NoteOperation:
    """A NOTE_TREE doc with ``recordType: 1`` — sync bookkeeping."""

    id: str
    rev: Optional[str]
    record_type: int
    commit_type: Optional[int]
    commit_status: Optional[int]
    document_unique_id: Optional[str]
    raw: Mapping[str, Any] = field(repr=False)


NoteRecord = Union[Note, NoteFolder, NoteOperation]


def _coerce(body: Mapping[str, Any]) -> NoteRecord:
    """Dispatch a doc body to the right typed shape.

    Operation records (``recordType: 1``) take priority over the
    ``document`` flag because the captured operation docs in
    ``notes-har-2026-05-31.har`` don't carry ``document`` at all —
    the discriminator is ``recordType``, not the absence of
    ``document``.
    """
    doc_id = body.get("_id") or body.get("uniqueId") or ""
    rev = body.get("_rev")

    if body.get("recordType") == 1:
        return NoteOperation(
            id=doc_id,
            rev=rev,
            record_type=1,
            commit_type=body.get("commitType"),
            commit_status=body.get("commitStatus"),
            document_unique_id=body.get("documentUniqueId"),
            raw=body,
        )

    common = dict(
        id=doc_id,
        rev=rev,
        title=body.get("title") or body.get("name"),
        parent_unique_id=body.get("parentUniqueId"),
        created_at=body.get("createdAt"),
        updated_at=body.get("updatedAt"),
        raw=body,
    )
    if body.get("document") is False:
        return NoteFolder(**common)

    page_name_list = body.get("pageNameList") or {}
    inner = page_name_list.get("pageNameList") if isinstance(page_name_list, Mapping) else None
    page_count = len(inner) if isinstance(inner, list) else body.get("pageCount")
    return Note(page_count=page_count, **common)


def _from_doc(body: Mapping[str, Any]) -> NoteRecord:
    """Public dispatcher; alias for :func:`_coerce`. Kept as a stable
    entry point in case callers want to coerce a raw body without
    going through the store."""
    return _coerce(body)


# Attach the classmethod-style dispatcher to each concrete type so
# ``Note.from_doc(body)`` reads as the documented entry point. They
# all delegate to the same coercer; which class the call lands on
# doesn't constrain the returned type.
Note.from_doc = staticmethod(_from_doc)  # type: ignore[attr-defined]
NoteFolder.from_doc = staticmethod(_from_doc)  # type: ignore[attr-defined]
NoteOperation.from_doc = staticmethod(_from_doc)  # type: ignore[attr-defined]


# --------------------------- channel name ----------------------------------


def _channel_name(client) -> str:
    uid = getattr(client, "userid", None)
    if not uid:
        raise ValueError(
            "client.userid is unset; call BooxClient with a live session "
            "(skip_init=False) or set userid manually before pull_notes()"
        )
    return f"{uid}{NOTE_TREE_SUFFIX}"


# --------------------------- pull loop -------------------------------------


def pull_notes(
    client,
    store: LocalStore,
    since: Optional[str] = None,
    longpoll: bool = False,
) -> Dict[str, Any]:
    """Pull the NOTE_TREE channel into ``store``.

    Flow
    ----

    1. Resolve ``since``: if ``None``, read ``store.get_checkpoint("NOTE_TREE")``.
       A still-``None`` value after that means "from the beginning".
    2. Call ``client.sync.changes(channel, since=since, longpoll=longpoll)``.
    3. Partition change records into ``(id, rev)`` pairs to fetch and
       deletions to hard-delete.
    4. ``client.sync.bulk_get`` the live pairs; upsert each successful
       body into the store under ``channel="NOTE_TREE"``.
    5. Hard-delete tombstoned rows.
    6. Advance the checkpoint to the new ``last_seq`` (only if the
       changes feed actually advanced — empty longpoll heartbeat returns
       the same seq we polled with, no need to rewrite).
    7. Return a summary dict.

    Returns
    -------

    ``{"fetched": <n>, "inserted": <n>, "deleted": <n>, "last_seq": <token>}``

    where ``fetched`` is the number of change records the server
    returned, ``inserted`` is the number of bodies successfully
    upserted, ``deleted`` is tombstones applied to the store, and
    ``last_seq`` is the persisted checkpoint token after this pull.
    """
    channel = _channel_name(client)

    if since is None:
        since = store.get_checkpoint(_LOCAL_CHANNEL)

    result = client.sync.changes(channel, since=since, longpoll=longpoll)

    fetched = len(result)
    if fetched == 0:
        if result.last_seq is not None and result.last_seq != since:
            store.set_checkpoint(_LOCAL_CHANNEL, result.last_seq)
        return {
            "fetched": 0,
            "inserted": 0,
            "deleted": 0,
            "last_seq": result.last_seq if result.last_seq is not None else since,
        }

    to_fetch: List[Mapping[str, str]] = []
    to_delete: List[str] = []
    for change in result:
        doc_id = change.get("id")
        if not doc_id:
            continue
        if change.get("deleted") or change.get("_deleted"):
            to_delete.append(doc_id)
            continue
        changes_list = change.get("changes") or []
        if not changes_list:
            continue
        rev = changes_list[0].get("rev")
        if not rev:
            continue
        to_fetch.append({"id": doc_id, "rev": rev})

    inserted = 0
    if to_fetch:
        bodies = client.sync.bulk_get(to_fetch)
        for entry in bodies:
            body = entry.get("ok")
            if body is None:
                # ``missing`` envelope — server doesn't have the rev anymore.
                continue
            if body.get("_deleted"):
                doc_id = body.get("_id")
                if doc_id:
                    to_delete.append(doc_id)
                continue
            doc_id = body.get("_id")
            rev = body.get("_rev")
            if not doc_id or not rev:
                continue
            store.upsert_doc(_LOCAL_CHANNEL, doc_id, rev, body)
            inserted += 1

    deleted = 0
    for doc_id in to_delete:
        cur = store._conn.execute(
            "DELETE FROM docs WHERE channel=? AND doc_id=?",
            (_LOCAL_CHANNEL, doc_id),
        )
        deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    if result.last_seq is not None:
        store.set_checkpoint(_LOCAL_CHANNEL, result.last_seq)

    return {
        "fetched": fetched,
        "inserted": inserted,
        "deleted": deleted,
        "last_seq": result.last_seq,
    }


# --------------------------- typed queries ---------------------------------


def iter_notes(store: LocalStore) -> Iterator[NoteRecord]:
    """Yield typed :class:`Note` / :class:`NoteFolder` records.

    Operation records (``recordType: 1``) are filtered out — callers
    asking "what notes do I have" almost never want bookkeeping
    entries. To inspect operations, iterate ``store.iter_channel("NOTE_TREE")``
    directly and dispatch with ``Note.from_doc`` per row.
    """
    for row in store.iter_channel(_LOCAL_CHANNEL):
        rec = _coerce(row["body"])
        if isinstance(rec, NoteOperation):
            continue
        yield rec


def get_note(store: LocalStore, note_id: str) -> Optional[NoteRecord]:
    """Return a single typed record by ``_id``, or ``None`` if absent.

    Unlike :func:`iter_notes`, this does *not* filter operation
    records — callers who asked for a specific id should get whatever
    is stored under it.
    """
    row = store.get_doc(_LOCAL_CHANNEL, note_id)
    if row is None:
        return None
    return _coerce(row["body"])
