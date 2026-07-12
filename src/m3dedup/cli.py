"""Command-line interface for m3dedup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import find_duplicates, open_db
from .scanner import scan_directory

DEFAULT_DB = str(Path.home() / "dedup.db")


def cmd_scan(args: argparse.Namespace) -> int:
    db_path = args.db
    conn = open_db(db_path)
    print(f"Scanning: {args.directory}")
    print(f"Database: {db_path}")
    count = scan_directory(args.directory, conn)
    conn.close()
    print(f"Done. {count} file(s) recorded.")
    return 0


def cmd_duplicates(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    groups = find_duplicates(conn)
    conn.close()

    if not groups:
        print("No duplicates found.")
        return 0

    total_dupes = 0
    for i, group in enumerate(groups, 1):
        size = group[0]["size_bytes"]
        print(f"\nGroup {i} — {len(group)} files, {size:,} bytes each:")
        for f in group:
            print(f"  {f['full_path']}")
        total_dupes += len(group)

    print(f"\n{len(groups)} duplicate group(s), {total_dupes} file(s) total.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="m3dedup",
        description="Simple file deduplication scanner with SQLite.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan a directory recursively")
    p_scan.add_argument("directory", help="Directory to scan")
    p_scan.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p_scan.set_defaults(func=cmd_scan)

    p_dupes = sub.add_parser("duplicates", help="List duplicate file groups")
    p_dupes.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p_dupes.set_defaults(func=cmd_duplicates)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
