"""Calibre bridge — Boox → Calibre reading-state write-back.

#39 ships the Phase 4 payoff: cross-reference Boox-side books to Calibre
by file MD5 (``Book.backend.md5`` ↔ Calibre book MD5) and write current
reading state (last read, progress, status, rating, favorite) into
Calibre custom columns so the "where am I?" answer lives in our
canonical book metadata.

Direction is read-side first (Boox → Calibre); the inverse push lives in
#40.

Calibre auth pattern
--------------------

We talk to Calibre through ``calibredb`` with the **Content Server URL
+ creds** form, never ``--library-path`` directly: the Calibre GUI holds
a lock on the local library file, so URL routing through the fleet's
auth layer is the only path that doesn't fight the GUI.

The four env vars (``CALIBRE_CONTENT_SERVER_URL``, ``CALIBRE_USERNAME``,
``CALIBRE_PASSWORD``, ``CALIBRE_LIBRARY_ID``) are pre-injected into the
agent's environment by the OpenClaw gateway. :class:`CalibreClient`
reads them at construction time; passing explicit kwargs overrides the
env for tests and per-library work.

URL form is ``http://host:port/#library_id`` — note the ``#``, not
``/``. ``calibredb`` parses the fragment as the library selector.

MD5 matching
------------

Matching is MD5-only this round (plan §Decisions #4 — revisit later
with a title+author fallback during a future books cleanup pass). Books
without a ``backend.md5`` on the Boox side are reported as unmatched,
not silently skipped. Calibre-side books with no Boox counterpart are
reported informationally so the operator can see drift.

Custom columns
--------------

The five columns we write are auto-created on first run via
``calibredb add_custom_column``. The call is idempotent in practice:
re-running against an existing column errors with a known message, which
we catch and treat as success.

Mapping (per the issue brief):

==========================  ========  =====================================
Calibre column              Type      Source
==========================  ========  =====================================
``#last_read``              datetime  ``book.last_access`` (epoch ms → ISO)
``#read_progress``          text      ``book.progress`` (e.g. ``"3/5"``)
``#reading_status``         text      ``book.reading_status`` via enum map
``#boox_rating``            int       ``book.rating`` (0-5)
``#favorite``               bool      ``book.favorite`` (0/1 → bool)
==========================  ========  =====================================

``reading_status`` enum (observed 2026-05-31):

- ``0`` → ``"unread"``
- ``1`` → ``"reading"``
- ``2`` → ``"finished"``
- anything else → ``"unknown:<n>"`` (preserves the raw int so a future
  live capture can extend the map without losing data).

Idempotency
-----------

:func:`sync_reading_state` compares each computed Calibre value against
the current value before writing. If all five match, the book is
counted as ``unchanged`` and no ``set_metadata`` call is made. Running
the sync twice in a row against an unchanged Boox library is therefore
a true no-op (zero writes), not a "writes the same data again" no-op.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from boox.sync.reader import Book, iter_books
from boox.sync.store import LocalStore


__all__ = [
    "BookMatch",
    "CalibreBook",
    "CalibreBridgeError",
    "CalibreClient",
    "MatchResult",
    "READING_STATUS_MAP",
    "SyncSummary",
    "map_reading_status",
    "match_books",
    "ms_to_iso8601",
    "sync_reading_state",
]


_log = logging.getLogger(__name__)


#: Boox ``readingStatus`` enum → Calibre ``#reading_status`` string.
#: Observed values during the 2026-05-31 HAR captures; the implementation
#: falls back to ``"unknown:<n>"`` for anything not in this table so a
#: future "abandoned" value (or similar) survives unchanged through the
#: sync and shows up plainly in the Calibre column.
READING_STATUS_MAP: Mapping[int, str] = {
    0: "unread",
    1: "reading",
    2: "finished",
}


#: The five custom columns we own. Order matters for the printed plan;
#: ``add_custom_column`` invocations also go in this order on first run.
_CUSTOM_COLUMNS: Sequence[Mapping[str, str]] = (
    {"name": "last_read", "label": "Last Read", "datatype": "datetime"},
    {"name": "read_progress", "label": "Read Progress", "datatype": "text"},
    {"name": "reading_status", "label": "Reading Status", "datatype": "text"},
    {"name": "boox_rating", "label": "Boox Rating", "datatype": "int"},
    {"name": "favorite", "label": "Favorite", "datatype": "bool"},
)


class CalibreBridgeError(Exception):
    """Raised when the calibredb CLI returns a non-recoverable error."""


# --------------------------- typed shapes ----------------------------------


@dataclass
class CalibreBook:
    """A row from ``calibredb list`` we care about for the bridge.

    Only the fields we actively use during matching/sync are surfaced
    here; the full row from ``calibredb`` carries many more fields that
    we leave in ``raw`` for callers who want them.
    """

    id: int
    md5: Optional[str]
    title: Optional[str] = None
    authors: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class BookMatch:
    """A successful Boox ↔ Calibre pairing via MD5."""

    boox_book_id: str
    calibre_book_id: int
    md5: str


@dataclass
class MatchResult:
    """Outcome of :func:`match_books`."""

    matched: List[BookMatch] = field(default_factory=list)
    unmatched_boox: List[Book] = field(default_factory=list)
    unmatched_calibre: List[CalibreBook] = field(default_factory=list)


@dataclass
class SyncSummary:
    """Outcome of :func:`sync_reading_state`."""

    updated: int = 0
    unchanged: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": list(self.errors),
        }


# --------------------------- helpers ---------------------------------------


def ms_to_iso8601(ms: Optional[int]) -> Optional[str]:
    """Convert an epoch-millisecond timestamp to an ISO 8601 UTC string.

    Returns ``None`` for ``None`` / non-integer / non-positive inputs so
    a missing ``lastAccess`` doesn't crash the bridge.
    """
    if ms is None:
        return None
    try:
        ms_int = int(ms)
    except (TypeError, ValueError):
        return None
    if ms_int <= 0:
        return None
    dt = datetime.fromtimestamp(ms_int / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def map_reading_status(value: Any) -> Optional[str]:
    """Map a Boox ``readingStatus`` enum value to a Calibre string."""
    if value is None:
        return None
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return f"unknown:{value!r}"
    mapped = READING_STATUS_MAP.get(as_int)
    if mapped is None:
        return f"unknown:{as_int}"
    return mapped


def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def _book_to_field_values(book: Book) -> Dict[str, Any]:
    """Compute the five custom-column values for one Boox book.

    Centralised so :func:`sync_reading_state` can both write these and
    compare them against current Calibre state without re-doing the
    conversion in two places.
    """
    return {
        "#last_read": ms_to_iso8601(book.last_access),
        "#read_progress": book.progress if book.progress is not None else None,
        "#reading_status": map_reading_status(book.reading_status),
        "#boox_rating": (
            int(book.rating) if book.rating not in (None, "") else None
        ),
        "#favorite": _to_bool(book.favorite),
    }


# --------------------------- CalibreClient ---------------------------------


class CalibreClient:
    """Thin ``calibredb`` wrapper for the URL + creds auth pattern.

    Most methods shell out to ``calibredb``; ``list_books`` parses
    ``--for-machine`` JSON, and ``list_custom_columns`` parses the
    machine-readable form similarly.

    Parameters
    ----------
    server_url, username, password, library_id
        Override the env-injected values. ``None`` (default) reads from
        ``CALIBRE_CONTENT_SERVER_URL`` / ``CALIBRE_USERNAME`` /
        ``CALIBRE_PASSWORD`` / ``CALIBRE_LIBRARY_ID``.
    calibredb_path
        Override the ``calibredb`` executable name. Useful for tests
        that point at a stub script.
    runner
        Injection seam for tests. A callable ``(argv: list[str]) ->
        subprocess.CompletedProcess`` that runs the command. Defaults to
        ``subprocess.run`` with text capture and ``check=False`` so the
        client can interpret returncodes itself.
    """

    def __init__(
        self,
        server_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        library_id: Optional[str] = None,
        calibredb_path: str = "calibredb",
        runner: Optional[Any] = None,
    ) -> None:
        self.server_url = server_url or os.environ.get("CALIBRE_CONTENT_SERVER_URL")
        self.username = username or os.environ.get("CALIBRE_USERNAME")
        self.password = password or os.environ.get("CALIBRE_PASSWORD")
        self.library_id = library_id or os.environ.get("CALIBRE_LIBRARY_ID")
        if not self.server_url or not self.username or self.password is None:
            raise CalibreBridgeError(
                "CalibreClient: missing one of "
                "CALIBRE_CONTENT_SERVER_URL / CALIBRE_USERNAME / "
                "CALIBRE_PASSWORD; pass explicit args or fix env injection"
            )
        if not self.library_id:
            raise CalibreBridgeError(
                "CalibreClient: missing CALIBRE_LIBRARY_ID (e.g. 'Books'); "
                "pass library_id= explicitly or set the env var"
            )
        self.calibredb_path = calibredb_path
        self._runner = runner or _default_runner

    # ---------- argv shaping ----------

    @property
    def with_library(self) -> str:
        return f"{self.server_url}/#{self.library_id}"

    def _auth_argv(self) -> List[str]:
        return [
            "--with-library",
            self.with_library,
            "--username",
            self.username or "",
            "--password",
            self.password or "",
        ]

    def _run(self, subcmd: str, *args: str) -> subprocess.CompletedProcess:
        argv = [self.calibredb_path, subcmd, *self._auth_argv(), *args]
        return self._runner(argv)

    @staticmethod
    def _scrub(argv: Sequence[str]) -> str:
        """Build a copy-pasteable command string with the password redacted.

        Used in error messages and debug logging. Never log
        ``self._auth_argv()`` raw — the password is in there.
        """
        scrubbed: List[str] = []
        skip_next = False
        for token in argv:
            if skip_next:
                scrubbed.append("***")
                skip_next = False
                continue
            scrubbed.append(token)
            if token == "--password":
                skip_next = True
        return " ".join(shlex.quote(t) for t in scrubbed)

    # ---------- public surface ----------

    def list_books(self, fields: Sequence[str] = ("id", "title", "authors")) -> List[CalibreBook]:
        """Return every book in the library with at least its MD5.

        ``identifiers`` is requested unconditionally because Calibre
        ships file MD5s as ``identifiers`` entries (``md5:abc...``) when
        present; some libraries also surface MD5 as a column directly,
        in which case ``raw['md5']`` is populated by Calibre too.
        """
        wanted = list(fields)
        for required in ("id", "identifiers"):
            if required not in wanted:
                wanted.append(required)
        result = self._run(
            "list",
            "--for-machine",
            "--fields",
            ",".join(wanted),
        )
        if result.returncode != 0:
            raise CalibreBridgeError(
                f"calibredb list failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CalibreBridgeError(
                f"calibredb list returned non-JSON output: {exc}"
            ) from exc

        out: List[CalibreBook] = []
        for row in rows:
            out.append(
                CalibreBook(
                    id=int(row.get("id")),
                    md5=_extract_md5(row),
                    title=row.get("title"),
                    authors=row.get("authors"),
                    raw=row,
                )
            )
        return out

    def list_custom_columns(self) -> List[Dict[str, Any]]:
        """Return the library's custom column definitions.

        Uses ``custom_columns --details``. ``calibredb`` does not have a
        machine-readable mode for this subcommand; we parse the
        ``name (#label, datatype)`` line format. Tests stub this out to
        return a known list rather than reproducing the parser.
        """
        result = self._run("custom_columns", "--details")
        if result.returncode != 0:
            raise CalibreBridgeError(
                f"calibredb custom_columns failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        out: List[Dict[str, Any]] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("---"):
                continue
            # Format is roughly: "label (#name, datatype, ...)"
            # We only need the lookup name (the bit after '#'). Fall
            # back to the whole line as the name when no '#' present.
            hash_idx = line.find("#")
            if hash_idx == -1:
                continue
            tail = line[hash_idx + 1 :]
            # name runs until the next ',' or ')' or whitespace
            name = ""
            for ch in tail:
                if ch in ",) \t":
                    break
                name += ch
            if name:
                out.append({"name": name, "raw": line})
        return out

    def add_custom_column(self, name: str, label: str, datatype: str) -> None:
        """Create a custom column. Treats "already exists" as success.

        Calibre's ``add_custom_column`` errors with a message containing
        ``already exists`` when the column is present; we catch that
        explicitly so re-running the bridge after a partial first run
        doesn't blow up.
        """
        result = self._run("add_custom_column", name, label, datatype)
        if result.returncode == 0:
            return
        stderr = (result.stderr or "") + (result.stdout or "")
        if "already exists" in stderr.lower():
            _log.debug("Calibre custom column #%s already exists; skipping", name)
            return
        raise CalibreBridgeError(
            f"calibredb add_custom_column #{name} failed "
            f"(rc={result.returncode}): {stderr.strip()}"
        )

    def set_metadata(
        self,
        book_id: int,
        fields: Mapping[str, Any],
    ) -> None:
        """Write one or more fields to a book via ``calibredb set_metadata``.

        ``fields`` is keyed by Calibre column name (``#last_read`` etc.).
        ``None`` values are skipped (you can't clear a Calibre custom
        column from the CLI in a single shot anyway — Calibre treats
        empty values inconsistently across datatypes).
        """
        args: List[str] = [str(book_id)]
        wrote_any = False
        for col, value in fields.items():
            if value is None:
                continue
            args += ["--field", f"{col}:{_format_field_value(value)}"]
            wrote_any = True
        if not wrote_any:
            return
        result = self._run("set_metadata", *args)
        if result.returncode != 0:
            raise CalibreBridgeError(
                f"calibredb set_metadata id={book_id} failed "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )

    def get_book_fields(self, book_id: int, fields: Sequence[str]) -> Dict[str, Any]:
        """Read a single book's current field values via ``calibredb list``.

        Filters with ``--search id:<n>`` to pull exactly one row.
        Returns ``{}`` if the book doesn't exist; raises on CLI failure.
        """
        result = self._run(
            "list",
            "--for-machine",
            "--search",
            f"id:{book_id}",
            "--fields",
            ",".join(fields),
        )
        if result.returncode != 0:
            raise CalibreBridgeError(
                f"calibredb list (id={book_id}) failed "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CalibreBridgeError(
                f"calibredb list (id={book_id}) returned non-JSON: {exc}"
            ) from exc
        if not rows:
            return {}
        return dict(rows[0])


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )


def _format_field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _extract_md5(row: Mapping[str, Any]) -> Optional[str]:
    """Pull a file MD5 out of a Calibre ``list`` row.

    Calibre puts MD5 in one of two places depending on library config:

    - ``identifiers`` (a dict like ``{"md5": "abc..."}`` or a list of
      ``"md5:abc..."`` strings).
    - A direct ``md5`` column if the operator added one as a custom
      column already.

    We try both, lowercase, and strip whitespace. Returns ``None`` if
    neither path yields a value.
    """
    direct = row.get("md5")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().lower()

    identifiers = row.get("identifiers")
    if isinstance(identifiers, Mapping):
        md5 = identifiers.get("md5")
        if isinstance(md5, str) and md5.strip():
            return md5.strip().lower()
    elif isinstance(identifiers, list):
        for entry in identifiers:
            if isinstance(entry, str) and entry.lower().startswith("md5:"):
                return entry.split(":", 1)[1].strip().lower()
    elif isinstance(identifiers, str):
        # Some Calibre versions return ``"md5:abc,isbn:..."``
        for part in identifiers.split(","):
            part = part.strip()
            if part.lower().startswith("md5:"):
                return part.split(":", 1)[1].strip().lower()
    return None


# --------------------------- match & sync ----------------------------------


def match_books(
    local_store: LocalStore,
    calibre_client: CalibreClient,
) -> MatchResult:
    """Join Boox books to Calibre books by ``backend.md5``.

    Boox books with no MD5 are reported as ``unmatched_boox`` rather
    than silently dropped — a missing MD5 is operationally interesting
    (the device hasn't computed one yet, or the doc is malformed).
    """
    calibre_books = calibre_client.list_books()
    by_md5: Dict[str, CalibreBook] = {}
    for cb in calibre_books:
        if cb.md5:
            by_md5.setdefault(cb.md5, cb)

    matched: List[BookMatch] = []
    unmatched_boox: List[Book] = []
    seen_calibre_ids: set = set()

    for book in iter_books(local_store):
        md5 = book.backend.md5.lower().strip() if (book.backend and book.backend.md5) else None
        if not md5:
            unmatched_boox.append(book)
            continue
        cb = by_md5.get(md5)
        if cb is None:
            unmatched_boox.append(book)
            continue
        matched.append(BookMatch(boox_book_id=book.id, calibre_book_id=cb.id, md5=md5))
        seen_calibre_ids.add(cb.id)

    unmatched_calibre = [cb for cb in calibre_books if cb.id not in seen_calibre_ids]

    return MatchResult(
        matched=matched,
        unmatched_boox=unmatched_boox,
        unmatched_calibre=unmatched_calibre,
    )


def _ensure_custom_columns(calibre_client: CalibreClient) -> None:
    """Create any of the five expected columns that aren't present yet."""
    existing = {col["name"] for col in calibre_client.list_custom_columns()}
    for spec in _CUSTOM_COLUMNS:
        if spec["name"] in existing:
            continue
        calibre_client.add_custom_column(spec["name"], spec["label"], spec["datatype"])


def _values_equal(current: Any, planned: Any) -> bool:
    """Equality check tolerant of Calibre's stringy round-tripping.

    ``calibredb list --for-machine`` returns ints for int columns and
    strings for text columns, but datetime columns can come back as
    either ISO strings or epoch seconds depending on Calibre version.
    We coerce both sides to strings and compare; that's loose enough to
    not generate spurious writes but strict enough to catch real diffs.
    """
    if current is None and planned is None:
        return True
    if current is None or planned is None:
        return False
    if isinstance(planned, bool):
        # Calibre returns "true"/"false" as strings or 0/1 as ints.
        return _to_bool(current) == planned
    return str(current).strip() == str(planned).strip()


def sync_reading_state(
    local_store: LocalStore,
    calibre_client: CalibreClient,
    *,
    dry_run: bool = False,
    match_result: Optional[MatchResult] = None,
) -> SyncSummary:
    """Write reading state to Calibre for every matched book.

    Parameters
    ----------
    local_store
        Source of Boox-side book records (via :func:`iter_books`).
    calibre_client
        Target Calibre instance.
    dry_run
        When ``True``, compute the plan but don't write. Custom columns
        are still ensured (otherwise the dry-run output would lie about
        idempotency on a first run); pass a stub client if you need a
        truly read-only dry run.
    match_result
        Optional pre-computed :class:`MatchResult`. Pass when the caller
        already ran :func:`match_books` and wants to display the
        unmatched lists separately; default is to compute fresh here.
    """
    summary = SyncSummary()

    if not dry_run:
        _ensure_custom_columns(calibre_client)

    if match_result is None:
        match_result = match_books(local_store, calibre_client)

    # Build {boox_id: Book} for O(1) lookup; iter_books is a generator
    # so we materialise once.
    books_by_id: Dict[str, Book] = {b.id: b for b in iter_books(local_store)}

    field_keys = [spec["name"] for spec in _CUSTOM_COLUMNS]
    read_fields = [f"#{name}" for name in field_keys]

    for match in match_result.matched:
        book = books_by_id.get(match.boox_book_id)
        if book is None:
            # Race condition: the store was mutated between match_books
            # and this loop. Surface but don't crash.
            summary.errors.append(
                {"boox_book_id": match.boox_book_id, "error": "book vanished from store mid-sync"}
            )
            continue

        planned = _book_to_field_values(book)

        try:
            current = calibre_client.get_book_fields(
                match.calibre_book_id, read_fields
            )
        except CalibreBridgeError as exc:
            summary.errors.append(
                {
                    "boox_book_id": book.id,
                    "calibre_book_id": match.calibre_book_id,
                    "error": str(exc),
                }
            )
            continue

        changed = {
            col: val
            for col, val in planned.items()
            if not _values_equal(current.get(col), val)
        }

        if not changed:
            summary.unchanged += 1
            continue

        if dry_run:
            _log.info(
                "[dry-run] would update calibre id=%s with %s",
                match.calibre_book_id,
                {k: v for k, v in changed.items()},
            )
            summary.updated += 1
            continue

        try:
            calibre_client.set_metadata(match.calibre_book_id, changed)
            summary.updated += 1
        except CalibreBridgeError as exc:
            summary.errors.append(
                {
                    "boox_book_id": book.id,
                    "calibre_book_id": match.calibre_book_id,
                    "error": str(exc),
                }
            )

    return summary
