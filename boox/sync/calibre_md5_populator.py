"""Populate Calibre books' ``identifier:md5:<hash>`` from book files.

Walks every book in a Calibre library via :class:`CalibreClient`,
downloads its primary format from the Content Server, computes the
full-file MD5 streaming (so large books don't blow up memory), and
writes the hash back as a Calibre identifier via
``calibredb set_metadata --field identifiers:md5:<hash>``.

This is a **one-time data prerequisite** for the Boox → Calibre
matching bridge (see ``boox/sync/calibre_bridge.py``). Vanilla
Calibre does not auto-compute file MD5s; identifiers are user-
populated. Without ``md5:`` identifiers on the Calibre side, the
bridge's MD5-only join surfaces zero matches. Run this once per
library; re-runs are idempotent (skip books that already have it).

Wire path
---------

Files are fetched from the Calibre Content Server's
``/get/<format>/<book_id>/<library_id>`` endpoint, authenticated
with HTTP digest using the same creds as the rest of the bridge.
``calibredb set_metadata --field identifiers:md5:<hash>`` adds the
``md5`` identifier without disturbing other identifier types
already present on the book (per Calibre's per-type identifier
update semantics).

Out of scope
------------

- KOReader partial-MD5 (different algorithm, different storage; see
  ``Great-Sarak/fluffy-fox#39`` for the CWA evaluation).
- Title+author fallback matching (per ``onyx-send2boox#67`` §Decisions #4).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence

import requests
from requests.auth import HTTPDigestAuth

from boox.sync.calibre_bridge import CalibreBook, CalibreBridgeError, CalibreClient


__all__ = [
    "PopulateResult",
    "PopulateError",
    "DEFAULT_FORMAT_PRIORITY",
    "DOWNLOAD_CHUNK_BYTES",
    "compute_md5_streaming",
    "pick_format",
    "format_url",
    "populate_md5_identifiers",
]


_log = logging.getLogger(__name__)


# 1 MiB chunks balance HTTP round-trip overhead with memory use; books
# can be hundreds of MB so we never want to read them whole.
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


# Preference order when a book has multiple formats. EPUB first because
# it's the smallest typical case and most universally available; PDF
# next because Boox sideloaded PDFs are common; AZW3 / MOBI / etc.
# behind. Lowercase comparison; calibredb returns formats as
# uppercase-extension strings.
DEFAULT_FORMAT_PRIORITY: Sequence[str] = (
    "epub",
    "pdf",
    "azw3",
    "mobi",
    "kepub",
    "cbz",
    "cbr",
    "txt",
)


@dataclass
class PopulateResult:
    """Outcome of a populator run.

    Counts are over books considered. ``skipped`` covers both
    "already has md5" and "no usable format" cases — those are
    further broken out in the ``raw`` list for callers that want
    the detail.
    """

    populated: int = 0
    skipped_already_present: int = 0
    skipped_no_format: int = 0
    errors: List[Mapping[str, Any]] = field(default_factory=list)
    dry_run_plan: List[Mapping[str, Any]] = field(default_factory=list)

    @property
    def total_skipped(self) -> int:
        return self.skipped_already_present + self.skipped_no_format

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "populated": self.populated,
            "skipped_already_present": self.skipped_already_present,
            "skipped_no_format": self.skipped_no_format,
            "errors": list(self.errors),
            "dry_run_plan": list(self.dry_run_plan),
        }


class PopulateError(CalibreBridgeError):
    """Raised for fatal populator failures.

    Per-book failures (one bad format, one set_metadata error) are
    recorded in :attr:`PopulateResult.errors` rather than raised;
    this exception is reserved for setup-level failures (no books,
    no auth, library not reachable) where continuing is pointless.
    """


def compute_md5_streaming(
    url: str,
    auth: HTTPDigestAuth,
    *,
    chunk_size: int = DOWNLOAD_CHUNK_BYTES,
    timeout: float = 60.0,
    session: Optional[requests.Session] = None,
) -> str:
    """Stream a file from ``url`` and return its full-file MD5 hex.

    Streams chunked through ``hashlib.md5().update()`` so memory use
    is bounded by ``chunk_size`` regardless of file size. Raises
    :class:`requests.HTTPError` on a non-2xx response.
    """
    h = hashlib.md5()
    requester = session.get if session is not None else requests.get
    with requester(url, auth=auth, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                h.update(chunk)
    return h.hexdigest()


def pick_format(
    formats: Iterable[Any],
    priority: Sequence[str] = DEFAULT_FORMAT_PRIORITY,
) -> Optional[str]:
    """Return the highest-priority format extension from ``formats``.

    ``formats`` from ``calibredb list --fields formats`` is one of:

    - **Bare format names** like ``["EPUB", "PDF"]`` — what a remote
      Content Server (``--with-library URL``) returns.
    - **Full on-disk paths** like
      ``["/Library/Author/Title (42)/Title - Author.epub"]`` — what
      a local-library calibredb run returns.

    Both shapes are handled: if an entry has a path separator or a
    ``.`` suffix, we extract the suffix; otherwise we treat the
    string as the extension directly. The result is lowercased and
    sorted by :data:`DEFAULT_FORMAT_PRIORITY`. If a book has only an
    unknown format we still return it (lowercased), so the populator
    can try fetching whatever's there rather than skipping silently.

    Returns ``None`` when ``formats`` is empty / None.
    """
    items: List[str] = []
    for fmt in formats or ():
        if not fmt:
            continue
        s = str(fmt)
        # If it looks like a path (slashes or a suffix) extract the
        # suffix; otherwise treat the whole token as the extension.
        suffix = Path(s).suffix.lstrip(".").lower()
        if suffix:
            items.append(suffix)
        else:
            ext = s.strip().lstrip(".").lower()
            if ext:
                items.append(ext)
    if not items:
        return None
    pri_index = {name: i for i, name in enumerate(priority)}
    items.sort(key=lambda ext: (pri_index.get(ext, len(priority)), ext))
    return items[0]


def format_url(
    server_url: str,
    library_id: str,
    book_id: int,
    fmt: str,
) -> str:
    """Build the Calibre Content Server file-download URL.

    Wire path (calibre docs / ``cps/server.py``):

    ``GET <server>/get/<format>/<book_id>/<library_id>``

    ``format`` is case-insensitive on the server; we lowercase
    consistently because that's what :func:`pick_format` returns.
    """
    return (
        f"{server_url.rstrip('/')}"
        f"/get/{fmt.lower()}/{book_id}/{library_id}"
    )


def populate_md5_identifiers(
    calibre: CalibreClient,
    *,
    dry_run: bool = False,
    skip_existing: bool = True,
    limit: Optional[int] = None,
    format_priority: Sequence[str] = DEFAULT_FORMAT_PRIORITY,
    session: Optional[requests.Session] = None,
    progress_callback: Optional[Any] = None,
) -> PopulateResult:
    """Walk the library and populate missing ``identifier:md5:<hash>``.

    Parameters
    ----------
    calibre
        Configured :class:`CalibreClient` pointing at the target
        library. Its ``username`` / ``password`` are reused for the
        Content Server HTTP fetch.
    dry_run
        If True, compute the would-be MD5s but skip the
        ``set_metadata`` write. Result's ``dry_run_plan`` is populated.
    skip_existing
        If True (default), books that already have an ``md5:``
        identifier are skipped without downloading. Set False to
        overwrite (e.g. after a file replace where the hash changed).
    limit
        If set, process at most this many books from the head of the
        list (useful for partial runs / testing).
    format_priority
        Override the default per-extension preference order.
    session
        Optional ``requests.Session`` for HTTP connection reuse.
    progress_callback
        Optional ``callable(processed: int, total: int, last_status: str)``
        invoked after each book. Useful for CLI runners that want to
        surface a running count.

    Returns
    -------

    A :class:`PopulateResult` with per-bucket counters and (in dry-run
    mode) the proposed (id, md5) plan.

    Per-book failures (download failure, set_metadata error, missing
    format) are recorded in ``result.errors`` and do not abort the
    run.
    """
    books = calibre.list_books(fields=("id", "title", "authors", "formats"))
    if not books:
        return PopulateResult()

    if limit is not None and limit >= 0:
        books = books[:limit]

    auth = HTTPDigestAuth(calibre.username or "", calibre.password or "")
    result = PopulateResult()
    total = len(books)

    for processed, book in enumerate(books, start=1):
        if skip_existing and book.md5:
            result.skipped_already_present += 1
            if progress_callback:
                progress_callback(processed, total, "skip:already-present")
            continue

        formats = book.raw.get("formats") or []
        ext = pick_format(formats, priority=format_priority)
        if not ext:
            result.skipped_no_format += 1
            if progress_callback:
                progress_callback(processed, total, "skip:no-format")
            continue

        url = format_url(
            calibre.server_url or "",
            calibre.library_id or "",
            book.id,
            ext,
        )
        try:
            md5 = compute_md5_streaming(url, auth, session=session)
        except Exception as exc:
            result.errors.append(
                {
                    "book_id": book.id,
                    "title": book.title,
                    "stage": "download_or_hash",
                    "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if progress_callback:
                progress_callback(processed, total, "error:download")
            continue

        if dry_run:
            result.dry_run_plan.append(
                {
                    "book_id": book.id,
                    "title": book.title,
                    "format": ext,
                    "md5": md5,
                }
            )
            result.populated += 1
            if progress_callback:
                progress_callback(processed, total, "dry-run:planned")
            continue

        try:
            calibre.set_metadata(book.id, {"identifiers": f"md5:{md5}"})
        except CalibreBridgeError as exc:
            result.errors.append(
                {
                    "book_id": book.id,
                    "title": book.title,
                    "stage": "set_metadata",
                    "md5": md5,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if progress_callback:
                progress_callback(processed, total, "error:set_metadata")
            continue

        result.populated += 1
        if progress_callback:
            progress_callback(processed, total, "populated")

    return result
