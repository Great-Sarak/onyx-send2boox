# SPDX-License-Identifier: MIT
"""``boox`` CLI entry point — argparse-based subcommand dispatcher.

This scaffold lands the dispatcher with zero subcommands wired. Subsequent
issues (#27 auth, #28 errors, #29 pushread, etc.) register their own
subparsers here as they add their module surfaces. Invoking ``boox`` with
no arguments prints help; ``boox --version`` prints the package version.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from boox._version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boox",
        description=(
            "Boox cloud client — push files, pages, and subscriptions; "
            "manage BooxDrop, PushRead, and library state."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"boox {__version__}",
    )

    # Subparsers — each module issue (#27, #28, #29, …) attaches its own
    # subcommand here. Kept around even when empty so the help text shows
    # the dispatcher shape that future commands will plug into.
    parser.add_subparsers(
        dest="command",
        metavar="<command>",
        title="commands",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 0

    # No subcommands are wired yet — anything that parses here is a bug in
    # this dispatcher. Once #27/#28/#29 land their subparsers each will
    # attach a ``func`` default and we'll dispatch through it.
    parser.error(f"unknown command: {args.command!r}")
    return 2  # pragma: no cover — argparse exits before reaching this


if __name__ == "__main__":
    raise SystemExit(main())
