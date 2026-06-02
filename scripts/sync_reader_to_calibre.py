#!/usr/bin/env python3
"""Sync Boox READER_LIBRARY reading state into Calibre custom columns.

End-to-end CLI driver for the bridge in ``boox/sync/calibre_bridge``:

1. Open the local sync store (``$BOOX_SYNC_DB`` or ``~/.cache/boox/sync.db``).
2. Build a :class:`CalibreClient` from env-injected creds (or ``--library``
   override for the library id).
3. Match books by MD5; sync reading state.
4. Print a friendly summary, including any unmatched books.

This script lives in the API repo (``onyx-send2boox``); the skill repo's
analog (``Great-Sarak/boox/scripts/sync_to_calibre.py``) is part of #9
and wraps this in the skill's usage UX.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from boox.sync import (
    CalibreBridgeError,
    CalibreClient,
    LocalStore,
    match_books,
    sync_reading_state,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Boox → Calibre reading-state sync. Reads from the local "
            "sync store; writes custom columns on matched Calibre books."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the sync SQLite DB (default: $BOOX_SYNC_DB or "
        "~/.cache/boox/sync.db).",
    )
    parser.add_argument(
        "--library",
        default=None,
        help="Calibre library id override (default: $CALIBRE_LIBRARY_ID, "
        "typically 'Books').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the plan and print the summary without writing.",
    )
    parser.add_argument(
        "--show-unmatched",
        action="store_true",
        help="Print details of every unmatched Boox/Calibre book.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        calibre = CalibreClient(library_id=args.library)
    except CalibreBridgeError as exc:
        print(f"calibre: {exc}", file=sys.stderr)
        return 2

    store = LocalStore(db_path=str(args.db) if args.db else None)

    matches = match_books(store, calibre)
    print(
        f"matched={len(matches.matched)} "
        f"unmatched_boox={len(matches.unmatched_boox)} "
        f"unmatched_calibre={len(matches.unmatched_calibre)}"
    )

    summary = sync_reading_state(
        store, calibre, dry_run=args.dry_run, match_result=matches
    )
    label = "[dry-run] " if args.dry_run else ""
    print(
        f"{label}updated={summary.updated} unchanged={summary.unchanged} "
        f"errors={len(summary.errors)}"
    )

    if summary.errors:
        for err in summary.errors:
            print(f"  ! {err}", file=sys.stderr)

    if args.show_unmatched:
        if matches.unmatched_boox:
            print("\nunmatched Boox books:")
            for book in matches.unmatched_boox:
                md5 = book.backend.md5 if book.backend else None
                print(f"  - {book.id}  name={book.name!r}  md5={md5}")
        if matches.unmatched_calibre:
            print("\nunmatched Calibre books:")
            for cb in matches.unmatched_calibre:
                print(f"  - id={cb.id}  title={cb.title!r}  md5={cb.md5}")

    return 0 if not summary.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
