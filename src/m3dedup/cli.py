"""Command-line interface for dedup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from .db import delete_file, find_duplicates, get_scanned_dirs, open_db
from .scanner import scan_directory
from .scanner_async import DEFAULT_CONCURRENCY, scan_directory_async
from datetime import datetime, timezone

DEFAULT_DB = str(Path.home() / "dedup.db")

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def _open_db_with_prompt(db_path: str):
    """Open the DB, prompting for confirmation if the file doesn't exist yet."""
    if not Path(db_path).exists():
        console = Console()
        console.print(f"[yellow]Database file does not exist:[/yellow] {db_path}")
        response = input("Create a new database? [y/N] ").strip().lower()
        if response != "y":
            console.print("[red]Aborted.[/red]")
            return None
    return open_db(db_path)


def human_size(n: int) -> str:
    """Format bytes as a human-readable string."""
    size = float(n)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} bytes"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def cmd_scan(args: argparse.Namespace) -> int:
    db_path = args.db
    conn = _open_db_with_prompt(db_path)
    if conn is None:
        return 1
    if not args.sync_mode:
        print(f"Scanning (async): {args.directory}")
        print(f"Database: {db_path}")
        print(f"Concurrency: {args.concurrency}")
        count = scan_directory_async(args.directory, conn, concurrency=args.concurrency)
    else:
        print(f"Scanning (sync): {args.directory}")
        print(f"Database: {db_path}")
        count = scan_directory(args.directory, conn)
    conn.close()
    print(f"Done. {count} file(s) recorded.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    console = Console()
    if not Path(args.db).exists():
        console.print(f"[red]Error:[/red] Database file does not exist: {args.db}")
        console.print("[dim]Run 'dedup scan <directory>' first to create a database.[/dim]")
        return 1
    conn = open_db(args.db)

    # Check if any scanned directory is older than 24 hours
    dirs = get_scanned_dirs(conn)
    now = datetime.now(timezone.utc)
    stale_dirs = []
    for d in dirs:
        try:
            scan_time = datetime.fromisoformat(d["scan_date"])
            if (now - scan_time).total_seconds() > 86400:
                stale_dirs.append(d)
        except (ValueError, TypeError):
            pass
    if stale_dirs:
        console.print("[bold red]⚠ WARNING: The following directories were scanned more than 24 hours ago.[/bold red]")
        console.print("[bold red]Results may be outdated. Run 'dedup rescan' to update.[/bold red]")
        for d in stale_dirs:
            console.print(f"[red]  {d['full_path']} (last scan: {d['scan_date']})[/red]")
        console.print()

    groups = find_duplicates(conn)
    conn.close()

    if not groups:
        if args.format == 2:
            print("[]")
        else:
            console.print("[green]No duplicates found.[/green]")
        return 0

    # Sort groups by file size descending (biggest first)
    groups.sort(key=lambda g: g[0]["size_bytes"], reverse=True)

    if args.format == 2:
        _show_json(groups)
    elif args.format == 1:
        _show_plain(groups)
    elif args.format == 3:
        _show_interactive(console, groups, conn)
    else:
        _show_rich(console, groups)
    conn.close()
    return 0


def _show_rich(console: Console, groups: list) -> None:
    """Format 0: rich console output (default)."""
    total_dupes = 0
    total_waste = 0
    for i, group in enumerate(groups, 1):
        size = group[0]["size_bytes"]
        waste = size * (len(group) - 1)
        total_waste += waste
        total_dupes += len(group)
        console.print(
            f"\n[bold cyan]Group {i}[/bold cyan] — "
            f"{len(group)} files, [bold]{human_size(size)}[/bold] each, "
            f"[dim]wasted: {human_size(waste)}[/dim]"
        )
        for f in group:
            console.print(f"  [dim]{f['full_path']}[/dim]")

    console.print(
        f"\n[bold]{len(groups)}[/bold] duplicate group(s), "
        f"[bold]{total_dupes}[/bold] file(s) total, "
        f"[bold red]{human_size(total_waste)}[/bold red] wasted."
    )


def _show_plain(groups: list) -> None:
    """Format 1: plain text output."""
    total_dupes = 0
    total_waste = 0
    for i, group in enumerate(groups, 1):
        size = group[0]["size_bytes"]
        waste = size * (len(group) - 1)
        total_waste += waste
        total_dupes += len(group)
        print(f"\nGroup {i} — {len(group)} files, {human_size(size)} each, wasted: {human_size(waste)}")
        for f in group:
            print(f"  {f['full_path']}")

    print(f"\n{len(groups)} duplicate group(s), {total_dupes} file(s) total, {human_size(total_waste)} wasted.")


def _show_json(groups: list) -> None:
    """Format 2: JSON output."""
    import json
    output = []
    for i, group in enumerate(groups, 1):
        size = group[0]["size_bytes"]
        output.append({
            "group": i,
            "file_count": len(group),
            "size_bytes": size,
            "size_human": human_size(size),
            "wasted_bytes": size * (len(group) - 1),
            "wasted_human": human_size(size * (len(group) - 1)),
            "files": [f["full_path"] for f in group],
        })
    print(json.dumps(output, indent=2))


def _show_interactive(console: Console, groups: list, conn) -> None:
    """Format 3: interactive dedup — ask which file to keep, delete the rest."""
    import os

    if not groups:
        console.print("[green]No duplicates found.[/green]")
        return

    total_deleted = 0
    total_freed = 0

    console.print(
        "\n[bold red]⚠ WARNING: Files will be deleted immediately when you choose which to keep. "
        "This cannot be undone.[/bold red]\n"
    )

    for i, group in enumerate(groups, 1):
        size = group[0]["size_bytes"]
        console.print(
            f"\n[bold cyan]Group {i}[/bold cyan] — "
            f"{len(group)} files, [bold]{human_size(size)}[/bold] each, "
            f"[dim]wasted: {human_size(size * (len(group) - 1))}[/dim]"
        )
        for j, f in enumerate(group):
            console.print(f"  [bold]{j + 1}[/bold]. {f['full_path']}")

        console.print("\n[bold yellow]Enter the number of the file to KEEP (all others will be deleted).[/bold yellow]")
        console.print("[bold yellow]Enter 's' to skip this group, or 'q' to quit.[/bold yellow]")

        choice = input("Keep which file? ").strip().lower()

        if choice == "q":
            console.print("[dim]Quitting.[/dim]")
            break
        if choice == "s":
            console.print("[dim]Skipping group.[/dim]")
            continue

        try:
            keep_idx = int(choice) - 1
            if keep_idx < 0 or keep_idx >= len(group):
                console.print("[red]Invalid number. Skipping group.[/red]")
                continue
        except ValueError:
            console.print("[red]Invalid input. Skipping group.[/red]")
            continue

        for j, f in enumerate(group):
            if j == keep_idx:
                continue
            try:
                os.remove(f["full_path"])
                delete_file(conn, f["full_path"])
                conn.commit()
                total_deleted += 1
                total_freed += size
                console.print(f"[green]  Deleted: {f['full_path']}[/green]")
            except OSError as exc:
                console.print(f"[red]  Error deleting {f['full_path']}: {exc}[/red]")

    if total_deleted:
        console.print(
            f"\n[bold green]Done. Deleted {total_deleted} file(s), freed {human_size(total_freed)}.[/bold green]"
        )
    else:
        console.print("\n[dim]No files deleted.[/dim]")


def cmd_rescan(args: argparse.Namespace) -> int:
    console = Console()
    conn = _open_db_with_prompt(args.db)
    if conn is None:
        return 1
    dirs = get_scanned_dirs(conn)

    if not dirs:
        console.print("[yellow]No directories have been scanned yet.[/yellow]")
        conn.close()
        return 0

    console.print(f"[bold]Rescanning {len(dirs)} directory(ies):[/bold]")
    for d in dirs:
        console.print(f"  [dim]{d['full_path']}[/dim]")

    if not args.sync_mode:
        scanner = lambda directory: scan_directory_async(directory, conn, concurrency=args.concurrency)
    else:
        scanner = lambda directory: scan_directory(directory, conn)

    total_count = 0
    for d in dirs:
        path = d["full_path"]
        console.print(f"\nScanning: {path}")
        try:
            count = scanner(path)
            total_count += count
            console.print(f"  {count} file(s) recorded.")
        except NotADirectoryError as exc:
            console.print(f"  [red]Skipping: {exc}[/red]")

    conn.close()
    console.print(f"\n[bold]Done. {total_count} file(s) recorded across {len(dirs)} director(ies).[/bold]")
    return 0


def cmd_dirs(args: argparse.Namespace) -> int:
    console = Console()
    if not Path(args.db).exists():
        console.print(f"[red]Error:[/red] Database file does not exist: {args.db}")
        console.print("[dim]Run 'dedup scan <directory>' first to create a database.[/dim]")
        return 1
    conn = open_db(args.db)
    dirs = get_scanned_dirs(conn)
    conn.close()

    if not dirs:
        console.print("[yellow]No directories have been scanned yet.[/yellow]")
        return 0

    console.print(f"[bold]{len(dirs)} scanned director(y/ies):[/bold]\n")
    for d in dirs:
        console.print(f"  [dim]{d['scan_date']}[/dim]  [bold]{d['full_path']}[/bold]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dedup",
        description=(
            "Simple file deduplication scanner with SQLite.\n\n"
            "Scans directories recursively, records file metadata (name, path, "
            "size, mtime, MD5 hash) into a SQLite database, and identifies "
            "duplicate files by matching hashes.\n\n"
            "Options per command:\n"
            "  --db PATH          SQLite database path (all commands, default: ~/dedup.db)\n"
            "  --sync             Use synchronous scanner (no concurrent hashing) (scan, rescan)\n"
            "  --concurrency N    Max files to hash in parallel (scan, rescan,\n"
            "                      default: min(32, CPU×4))"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser(
        "scan",
        help="Scan a directory recursively",
        description=(
            "Scan a directory recursively and record file metadata (name, path, "
            "size, mtime, MD5 hash) into the database. Files larger than 4 KB are "
            "hashed using partial hashing (first + last 4 KB); full hashes are "
            "computed only when partial hashes collide.\n\n"
            "Re-scanning the same directory updates existing entries (upsert)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_scan.add_argument("directory", help="Directory to scan")
    p_scan.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p_scan.add_argument(
        "--sync",
        dest="sync_mode",
        action="store_true",
        help="Use synchronous scanner (no concurrent hashing). Default is async with concurrent hashing.",
    )
    p_scan.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max files to hash in parallel (default: {DEFAULT_CONCURRENCY})",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_dupes = sub.add_parser(
        "show",
        help="List duplicate file groups",
        description=(
            "List groups of files with identical MD5 hashes. Groups are sorted by "
            "file size (largest first). Shows total duplicate count and wasted space."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_dupes.add_argument(
        "format",
        type=int,
        nargs="?",
        default=0,
        help="Output format: 0 = rich console (default), 1 = plain text, 2 = JSON, 3 = interactive dedup (delete files)",
    )
    p_dupes.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p_dupes.set_defaults(func=cmd_show)

    p_rescan = sub.add_parser(
        "rescan",
        help="Re-scan all previously scanned directories",
        description=(
            "Re-scan all directories previously recorded via 'scan'. Files whose "
            "mtime hasn't changed are skipped. Updated files are re-hashed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_rescan.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p_rescan.add_argument(
        "--sync",
        dest="sync_mode",
        action="store_true",
        help="Use synchronous scanner (no concurrent hashing). Default is async with concurrent hashing.",
    )
    p_rescan.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max files to hash in parallel (default: {DEFAULT_CONCURRENCY})",
    )
    p_rescan.set_defaults(func=cmd_rescan)

    p_dirs = sub.add_parser(
        "dirs",
        help="List all previously scanned directories",
        description="List all directories that have been scanned and recorded in the database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_dirs.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p_dirs.set_defaults(func=cmd_dirs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
