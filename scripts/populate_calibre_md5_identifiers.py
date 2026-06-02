#!/usr/bin/env python3
"""Populate ``identifier:md5:<hash>`` on Calibre books from their files.

CLI driver for :mod:`boox.sync.calibre_md5_populator`. One-time data
prerequisite for the Boox → Calibre matching bridge (see
``scripts/sync_reader_to_calibre.py``) — vanilla Calibre does not
auto-compute file MD5s, so the bridge's MD5-only join can't match
anything until this runs.

Usage
-----

Set ``CALIBRE_CONTENT_SERVER_URL`` / ``CALIBRE_USERNAME`` /
``CALIBRE_PASSWORD`` / ``CALIBRE_LIBRARY_ID`` (or override via flags),
then::

    # Dry run on the first 5 books to verify wiring
    python scripts/populate_calibre_md5_identifiers.py --dry-run --limit 5

    # Full run, skipping books that already have md5
    python scripts/populate_calibre_md5_identifiers.py

    # Overwrite even when md5 is already set (e.g. file replaced)
    python scripts/populate_calibre_md5_identifiers.py --no-skip-existing

After a successful run, :file:`scripts/sync_reader_to_calibre.py`'s
matched count should rise from zero to N (where N = books in both
Boox and Calibre by full-file MD5).
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from boox.sync import CalibreBridgeError, CalibreClient
from boox.sync.calibre_md5_populator import (
    DEFAULT_FORMAT_PRIORITY,
    populate_md5_identifiers,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compute file MD5 for every Calibre book and write it back "
            "as identifier:md5:<hash>. One-time prerequisite for the "
            "Boox -> Calibre matching bridge."
        ),
    )
    parser.add_argument(
        "--library",
        default=None,
        help=(
            "Calibre library id override "
            "(default: $CALIBRE_LIBRARY_ID, typically 'Books')."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute MD5s but skip the set_metadata write. Prints the "
            "planned (id, md5) for each book."
        ),
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        default=True,
        help=(
            "Overwrite books that already have an md5: identifier. "
            "Default: skip them (idempotent re-runs)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Process at most this many books from the head of the "
            "library list. Useful for partial runs / smoke tests."
        ),
    )
    parser.add_argument(
        "--format-priority",
        default=",".join(DEFAULT_FORMAT_PRIORITY),
        help=(
            "Comma-separated extension preference order when a book has "
            "multiple formats. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a running per-book status line.",
    )
    return parser


def _emit_progress(
    processed: int, total: int, status: str, *, verbose: bool
) -> None:
    if not verbose:
        return
    print(f"  [{processed:5d}/{total}] {status}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        calibre = CalibreClient(library_id=args.library)
    except CalibreBridgeError as exc:
        print(f"calibre: {exc}", file=sys.stderr)
        return 2

    print(
        f"library: {calibre.with_library}  "
        f"dry_run={args.dry_run}  skip_existing={args.skip_existing}  "
        f"limit={args.limit}"
    )

    priority = [
        ext.strip().lower()
        for ext in args.format_priority.split(",")
        if ext.strip()
    ]

    def progress(processed: int, total: int, status: str) -> None:
        _emit_progress(processed, total, status, verbose=args.verbose)

    result = populate_md5_identifiers(
        calibre,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        limit=args.limit,
        format_priority=priority,
        progress_callback=progress,
    )

    label = "[dry-run] " if args.dry_run else ""
    print(
        f"{label}populated={result.populated} "
        f"skipped_already_present={result.skipped_already_present} "
        f"skipped_no_format={result.skipped_no_format} "
        f"errors={len(result.errors)}"
    )

    if args.dry_run and result.dry_run_plan:
        print("\n[dry-run] proposed identifier writes:")
        for plan in result.dry_run_plan[:25]:
            print(
                f"  - id={plan['book_id']} "
                f"format={plan['format']} "
                f"md5={plan['md5']}  "
                f"title={plan['title']!r}"
            )
        if len(result.dry_run_plan) > 25:
            print(f"  ... and {len(result.dry_run_plan) - 25} more.")

    if result.errors:
        print(f"\nerrors ({len(result.errors)}):", file=sys.stderr)
        for err in result.errors[:25]:
            print(
                f"  ! id={err['book_id']} stage={err['stage']} "
                f"err={err['error']}",
                file=sys.stderr,
            )
        if len(result.errors) > 25:
            print(
                f"  ... and {len(result.errors) - 25} more errors.",
                file=sys.stderr,
            )

    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
