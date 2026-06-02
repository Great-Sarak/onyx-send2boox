"""READER_LIBRARY channel sync — pull book metadata + annotations.

#37 mirrors the per-channel sync loop from #36 (NOTE_TREE) for the
``<user_uid>-READER_LIBRARY`` channel: the cloud-side mirror of every
book the device has touched, plus their annotation docs and a sync
bookkeeping stream. After this module runs, ``iter_books`` and friends
return typed records the Calibre bridge (#39) can match against the
local Calibre library by ``backend.md5``.

This is metadata-only sync. Binary thumbnails and book files
(``/<user_uid>/reader/...`` OSS URLs) are out of scope — #38 handles
binary fetch. Annotation schema interpretation beyond the top-level
fields is also out of scope until we capture an annotation-create HAR
(Phase 5 follow-up).

Channel name
------------

``<user_uid>-READER_LIBRARY`` — assembled inside :func:`pull_library`
from ``client.userid``, same pattern as #36.

Doc kinds (HAR-confirmed against the 2026-05-31 library captures)
-----------------------------------------------------------------

READER_LIBRARY carries three discriminable kinds, dispatched by
:meth:`Book.from_doc`:

- ``recordType: 1`` → :class:`LibraryOperation`. Sync bookkeeping
  (delete / share ops against a book). The discriminator matches the
  NOTE_TREE pattern exactly.
- A ``documentId`` field referring back to a book ``_id`` (and no
  ``recordType``) → :class:`ReaderNote`. Annotation / highlight /
  handwritten-note container. The active-shape schema lives inside
  ``commitInfo`` as a JSON-encoded string — lazy-decoded the same way
  ``extraAttributes`` is on books.
- Everything else carrying ``UUID`` + ``extraAttributes`` →
  :class:`Book`. Top-level metadata (name, progress, reading status,
  …) plus a nested ``backend`` payload parsed lazily from
  ``extraAttributes``.

Why ``documentId`` and not ``document: false``? The 2026-05-31 HARs
show neither books nor reader-notes carry a ``document`` flag (that's a
NOTE_TREE-ism). ``documentId`` is the one field unique to reader-notes
and absent from book docs in every captured sample, so it's the
load-bearing discriminator here.

extraAttributes / commitInfo — lazy decode
------------------------------------------

Both ``Book.backend`` and ``ReaderNote.commit_info`` parse a
JSON-encoded string nested inside the doc body. Decoded once per
instance and cached:

- Parsing on access (property, not constructor) means we don't burn
  CPU on every doc the pull loop upserts — only the consumers that
  actually need a decoded backend pay.
- A malformed blob returns ``None`` and logs a warning rather than
  raising, so a single corrupt doc in the channel doesn't take the
  whole pull down. Books with no ``extraAttributes`` (or an empty
  string) also return ``None``.

Hard-delete policy
------------------

Mirrors the #36 §Decisions entry: ``deleted: true`` in a change record
or ``_deleted: true`` in a fetched body hard-deletes the local row.
This is a metadata mirror of current cloud state, not a backup;
callers needing a fresh view should re-pull with ``since=None``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union

from boox.sync.store import LocalStore


__all__ = [
    "Book",
    "BookBackend",
    "ReaderNote",
    "LibraryOperation",
    "Bookmark",
    "LibraryRecord",
    "READER_LIBRARY_SUFFIX",
    "pull_library",
    "iter_books",
    "iter_bookmarks_for_book",
    "get_book",
    "iter_reader_notes_for_book",
]


_log = logging.getLogger(__name__)


# Sentinel for "lazy-decode hasn't run yet" vs the legitimate
# decoded-to-None case. Using a module-private object keeps the
# three states distinguishable: unset, decoded-None, decoded-value.
_UNSET: Any = object()


READER_LIBRARY_SUFFIX = "-READER_LIBRARY"

#: Channel-table key under which READER_LIBRARY rows live in ``LocalStore``.
#: Same uid-stripping convention as NOTE_TREE — the cloud channel carries
#: the uid, the local channel is just the kind, so one DB can serve any
#: account without rewriting on user switch.
_LOCAL_CHANNEL = "READER_LIBRARY"


# --------------------------- typed shapes ----------------------------------


@dataclass
class BookBackend:
    """Parsed ``extraAttributes.backend`` payload.

    Fields documented in ``flora/BOOX.md`` §"extraAttributes — the
    reading-state goldmine". A handful of named accessors for the
    high-value fields (md5, current_page_position_v2, total_page,
    layout_type, viewport, …) plus ``raw`` for anything else the
    Boox firmware ships that we haven't promoted to a named field yet.
    """

    md5: Optional[str]
    current_page_position_v2: Optional[str]
    total_page: Optional[int]
    document_category: Optional[str]
    layout_type: Optional[str]
    last_dual_page_type: Optional[str]
    orientation: Optional[int]
    actual_scale: Optional[float]
    viewport: Optional[str]
    doc_id: Optional[str]
    raw: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BookBackend":
        return cls(
            md5=data.get("md5"),
            current_page_position_v2=data.get("current_page_position_v2"),
            total_page=data.get("total_page"),
            document_category=data.get("document_category"),
            layout_type=data.get("layout_type"),
            last_dual_page_type=data.get("last_dual_page_type"),
            orientation=data.get("orientation"),
            actual_scale=data.get("actual_scale"),
            viewport=data.get("viewport"),
            doc_id=data.get("doc_id"),
            raw=data,
        )


@dataclass
class Book:
    """A READER_LIBRARY doc representing a book metadata record.

    Top-level fields are pulled from the doc body directly; the rich
    reading-state payload (md5, position, viewport, layout prefs) lives
    inside ``extraAttributes`` and is exposed through the
    :attr:`backend` lazy property.
    """

    id: str
    rev: Optional[str]
    uuid: Optional[str]
    name: Optional[str]
    last_access: Optional[int]
    last_modified: Optional[int]
    progress: Optional[Any]
    reading_status: Optional[Any]
    rating: Optional[Any]
    favorite: Optional[Any]
    location: Optional[str]
    native_absolute_path: Optional[str]
    storage_id: Optional[str]
    size: Optional[int]
    id_string: Optional[str]
    hash_tag: Optional[str]
    file_sync_status: Optional[int]
    user_data_sync_status: Optional[int]
    raw: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        self._backend_cache: Any = _UNSET
        self._backend_warned: bool = False

    @property
    def backend(self) -> Optional[BookBackend]:
        """Lazy-decoded ``extraAttributes.backend`` payload.

        Returns ``None`` and logs a warning when the blob is missing or
        malformed. Cached on first access so subsequent reads are free.
        """
        if self._backend_cache is not _UNSET:
            return self._backend_cache
        raw = self.raw.get("extraAttributes")
        if not raw:
            self._backend_cache = None
            return None
        try:
            parsed = json.loads(raw)
            backend_dict = parsed.get("backend")
            if not isinstance(backend_dict, Mapping):
                self._backend_cache = None
                return None
            self._backend_cache = BookBackend.from_dict(backend_dict)
            return self._backend_cache
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            if not self._backend_warned:
                _log.warning(
                    "Book %s: malformed extraAttributes (%s); backend=None",
                    self.id,
                    exc.__class__.__name__,
                )
                self._backend_warned = True
            self._backend_cache = None
            return None


@dataclass
class ReaderNote:
    """A READER_LIBRARY doc representing an annotation/highlight container.

    Links to its parent :class:`Book` via :attr:`document_id`. The
    rich shape data lives inside ``commitInfo`` as a JSON-encoded
    string, exposed through the :attr:`commit_info` lazy property
    with the same malformed-tolerance as :attr:`Book.backend`.
    """

    id: str
    rev: Optional[str]
    uuid: Optional[str]
    document_id: Optional[str]
    title: Optional[str]
    current_shape_type: Optional[Any]
    background: Optional[Any]
    stroke_color: Optional[Any]
    stroke_width: Optional[Any]
    reader_note_page_name_map: Optional[Any]
    created_at: Optional[int]
    updated_at: Optional[int]
    raw: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        self._commit_cache: Any = _UNSET
        self._commit_warned: bool = False

    @property
    def commit_info(self) -> Optional[Any]:
        """Lazy-decoded ``commitInfo`` payload.

        Returns ``None`` and logs a warning on malformed JSON. Whatever
        structure Boox ships inside ``commitInfo`` is opaque to us —
        the Phase 5 annotation-schema HAR will tell us what to promote
        to named fields. Until then callers get the raw decoded value.
        """
        if self._commit_cache is not _UNSET:
            return self._commit_cache
        raw = self.raw.get("commitInfo")
        if raw is None or raw == "":
            self._commit_cache = None
            return None
        if not isinstance(raw, str):
            # Some captures might already carry it decoded; pass through.
            self._commit_cache = raw
            return raw
        try:
            self._commit_cache = json.loads(raw)
            return self._commit_cache
        except (json.JSONDecodeError, TypeError) as exc:
            if not self._commit_warned:
                _log.warning(
                    "ReaderNote %s: malformed commitInfo (%s); commit_info=None",
                    self.id,
                    exc.__class__.__name__,
                )
                self._commit_warned = True
            self._commit_cache = None
            return None


@dataclass
class LibraryOperation:
    """A READER_LIBRARY doc with ``recordType: 1`` — sync bookkeeping.

    The live channel surfaces these as per-page progress records
    (``commitType: 4``, ``commitStatus: 1``) — one per book the
    server has seen progress for. Older HAR captures from NOTE_TREE
    showed a leaner shape with only ``commitType``/``commitStatus``
    set; both shapes coerce to this class.
    """

    id: str
    rev: Optional[str]
    record_type: int
    commit_type: Optional[int]
    commit_status: Optional[int]
    document_unique_id: Optional[str]
    raw: Mapping[str, Any] = field(repr=False)


@dataclass
class Bookmark:
    """A READER_LIBRARY doc representing a single highlighted excerpt.

    Distinguished from :class:`ReaderNote` by the presence of a
    ``quote`` field — the highlighted text. Bookmarks reference their
    parent book by :attr:`document_id` and carry positional metadata
    (page number, byte/page-offset position, and an ``xpath`` for
    EPUB-style reflowable formats).
    """

    id: str
    rev: Optional[str]
    document_id: Optional[str]
    quote: Optional[str]
    page_number: Optional[int]
    position: Optional[Any]
    position_type: Optional[Any]
    xpath: Optional[str]
    title: Optional[str]
    created_at: Optional[int]
    updated_at: Optional[int]
    raw: Mapping[str, Any] = field(repr=False)


LibraryRecord = Union[Book, ReaderNote, LibraryOperation, Bookmark]


def _coerce(body: Mapping[str, Any]) -> LibraryRecord:
    """Dispatch a doc body to the right typed shape.

    Discriminator order matters — the channel is heterogeneous:

    1. ``recordType == 1`` → :class:`LibraryOperation`. Progress and
       commit-only sync records (``commitType==4`` for progress, other
       commit types for note-bookkeeping) — these are the only docs
       on this channel that explicitly carry ``recordType: 1``.
    2. ``"quote" in body`` → :class:`Bookmark`. Highlighted excerpts.
       Must be checked before the ``documentId`` branch because
       bookmarks also carry ``documentId`` (their parent book) and
       would otherwise mis-coerce to :class:`ReaderNote`.
    3. ``documentId`` present → :class:`ReaderNote`. Annotation /
       highlight containers (``debugInfo`` starts with
       ``SyncReaderNoteDocumentModel{...}`` on the live wire).
    4. Otherwise → :class:`Book`. Book metadata bodies carry no
       ``recordType``, no ``documentId``, no ``quote`` — instead they
       have ``name`` + ``nativeAbsolutePath`` + ``progress`` etc.
    """
    doc_id = body.get("_id") or body.get("uniqueId") or ""
    rev = body.get("_rev")

    if body.get("recordType") == 1:
        return LibraryOperation(
            id=doc_id,
            rev=rev,
            record_type=1,
            commit_type=body.get("commitType"),
            commit_status=body.get("commitStatus"),
            document_unique_id=body.get("documentUniqueId"),
            raw=body,
        )

    if "quote" in body:
        return Bookmark(
            id=doc_id,
            rev=rev,
            document_id=body.get("documentId"),
            quote=body.get("quote"),
            page_number=body.get("pageNumber"),
            position=body.get("position"),
            position_type=body.get("positionType"),
            xpath=body.get("xpath"),
            title=body.get("title"),
            created_at=body.get("createdAt"),
            updated_at=body.get("updatedAt"),
            raw=body,
        )

    if "documentId" in body:
        return ReaderNote(
            id=doc_id,
            rev=rev,
            uuid=body.get("uUID") or body.get("UUID"),
            document_id=body.get("documentId"),
            title=body.get("title"),
            current_shape_type=body.get("currentShapeType"),
            background=body.get("background"),
            stroke_color=body.get("strokeColor"),
            stroke_width=body.get("strokeWidth"),
            reader_note_page_name_map=body.get("readerNotePageNameMap"),
            created_at=body.get("createdAt"),
            updated_at=body.get("updatedAt"),
            raw=body,
        )

    return Book(
        id=doc_id,
        rev=rev,
        uuid=body.get("uUID") or body.get("UUID"),
        name=body.get("name"),
        last_access=body.get("lastAccess"),
        last_modified=body.get("lastModified"),
        progress=body.get("progress"),
        reading_status=body.get("readingStatus"),
        rating=body.get("rating"),
        favorite=body.get("favorite"),
        location=body.get("location"),
        native_absolute_path=body.get("nativeAbsolutePath"),
        storage_id=body.get("storageId"),
        size=body.get("size"),
        id_string=body.get("idString"),
        hash_tag=body.get("hashTag"),
        file_sync_status=body.get("fileSyncStatus"),
        user_data_sync_status=body.get("userDataSyncStatus"),
        raw=body,
    )


def _from_doc(body: Mapping[str, Any]) -> LibraryRecord:
    return _coerce(body)


Book.from_doc = staticmethod(_from_doc)  # type: ignore[assignment]
ReaderNote.from_doc = staticmethod(_from_doc)  # type: ignore[assignment]
LibraryOperation.from_doc = staticmethod(_from_doc)  # type: ignore[assignment]
Bookmark.from_doc = staticmethod(_from_doc)  # type: ignore[assignment]


# --------------------------- channel name ----------------------------------


def _channel_name(client) -> str:
    uid = getattr(client, "userid", None)
    if not uid:
        raise ValueError(
            "client.userid is unset; call BooxClient with a live session "
            "(skip_init=False) or set userid manually before pull_library()"
        )
    return f"{uid}{READER_LIBRARY_SUFFIX}"


# --------------------------- pull loop -------------------------------------


def pull_library(
    client,
    store: LocalStore,
    since: Optional[str] = None,
    longpoll: bool = False,
) -> Dict[str, Any]:
    """Pull the READER_LIBRARY channel into ``store``.

    Same shape as :func:`boox.sync.notes.pull_notes`:

    1. Resolve ``since``: ``None`` falls back to the persisted
       ``store.get_checkpoint("READER_LIBRARY")``; still-``None`` means
       "from the beginning".
    2. ``client.sync.changes(channel, since=since, longpoll=longpoll)``.
    3. Partition into live ``(id, rev)`` pairs and tombstones.
    4. ``client.sync.bulk_get`` the live pairs; upsert each into the
       store under ``channel="READER_LIBRARY"``.
    5. Hard-delete tombstoned rows.
    6. Advance the checkpoint (only if ``last_seq`` actually moved).

    Returns
    -------

    ``{"fetched": <n>, "inserted": <n>, "deleted": <n>, "last_seq": <token>}``
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


def iter_books(store: LocalStore) -> Iterator[Book]:
    """Yield typed :class:`Book` records from the store.

    Reader-notes and operation records are filtered out — callers
    asking "what books do I have" almost never want annotation
    containers or sync bookkeeping mixed in. Use
    :func:`iter_reader_notes_for_book` for annotations and iterate
    ``store.iter_channel("READER_LIBRARY")`` directly for ops.
    """
    for row in store.iter_channel(_LOCAL_CHANNEL):
        rec = _coerce(row["body"])
        if isinstance(rec, Book):
            yield rec


def get_book(store: LocalStore, book_id: str) -> Optional[Book]:
    """Return a single :class:`Book` by ``_id``, or ``None`` if absent.

    Unlike :func:`iter_books` this returns ``None`` rather than a
    typed-union result when the row exists but isn't a :class:`Book` —
    a caller asking ``get_book("...")`` is asking for a book, not
    "whatever happens to live at that id." For typed-union access
    against an arbitrary id, use ``Book.from_doc(store.get_doc(...)["body"])``.
    """
    row = store.get_doc(_LOCAL_CHANNEL, book_id)
    if row is None:
        return None
    rec = _coerce(row["body"])
    if not isinstance(rec, Book):
        return None
    return rec


def iter_reader_notes_for_book(
    store: LocalStore, book_id: str
) -> Iterator[ReaderNote]:
    """Yield :class:`ReaderNote` records whose ``documentId`` matches.

    Books and reader-notes can share the same ``<user_uid>#<UUID>``
    ``_id`` shape, but a reader-note always carries a ``documentId``
    pointing at its parent book's ``_id``. Filter by exact match.
    """
    for row in store.iter_channel(_LOCAL_CHANNEL):
        rec = _coerce(row["body"])
        if isinstance(rec, ReaderNote) and rec.document_id == book_id:
            yield rec


def iter_bookmarks_for_book(
    store: LocalStore, book_id: str
) -> Iterator[Bookmark]:
    """Yield :class:`Bookmark` records whose ``documentId`` matches.

    Bookmarks reference their parent book by ``documentId``, same
    shape as :class:`ReaderNote`. The discriminator at coerce time
    is the presence of a ``quote`` field — this iterator filters by
    type after coercion.
    """
    for row in store.iter_channel(_LOCAL_CHANNEL):
        rec = _coerce(row["body"])
        if isinstance(rec, Bookmark) and rec.document_id == book_id:
            yield rec
