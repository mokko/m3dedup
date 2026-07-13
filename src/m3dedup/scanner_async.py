"""Async file scanning and hashing logic.

Two-phase approach (same as the sync scanner):
  1. Partial hash: hash only the first + last 4 KB of large files.
  2. Resolve: for files sharing the same (size, partial hash), compute
     the full MD5 to confirm true duplicates.

File hashing is I/O-bound, so each file is hashed in a thread via
``asyncio.to_thread``. A semaphore limits how many files are open at once.
SQLite writes happen on the main thread after each file is hashed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .db import add_scanned_dir, insert_file
from .progress import count_files, make_progress
from .scanner import resolve_collisions, resolve_hashes_async

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = min(32, (os.cpu_count() or 4) * 4)


async def _scan_one(
    full: Path,
    scan_date: str,
    conn,
    sem: asyncio.Semaphore,
) -> bool:
    """Compute partial hash for a single file and insert into the DB."""
    async with sem:
        try:
            stat = full.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            partial, full_hash = await resolve_hashes_async(full, stat, conn)

            insert_file(
                conn,
                filename=full.name,
                full_path=str(full),
                mtime=mtime,
                size_bytes=stat.st_size,
                md5_hash=full_hash,
                scan_date=scan_date,
                md5_partial=partial,
            )
            return True
        except (OSError, PermissionError) as exc:
            log.warning("Skipping %s: %s", full, exc)
            return False


async def _scan_directory_async(
    directory: Path,
    conn,
    concurrency: int,
) -> tuple[int, str]:
    scan_date = datetime.now(timezone.utc).isoformat()
    sem = asyncio.Semaphore(concurrency)
    total = count_files(directory)
    count = 0

    with make_progress() as progress:
        task = progress.add_task("scan", total=total)

        # Collect all file paths first (os.walk is synchronous but fast)
        tasks: list[asyncio.Task] = []
        for root, _dirs, files in os.walk(directory):
            for name in files:
                full = Path(root) / name
                tasks.append(asyncio.create_task(_scan_one(full, scan_date, conn, sem)))

        # Process results as they complete for progress feedback
        for coro in asyncio.as_completed(tasks):
            ok = await coro
            if ok:
                count += 1
            progress.advance(task)

    conn.commit()
    return count, scan_date


def scan_directory_async(
    directory: str | Path,
    conn,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> int:
    """
    Scan *directory* recursively and insert every file into the database
    using async I/O for concurrent partial hashing.

    Phase 1: compute partial hashes concurrently.
    Phase 2: resolve collisions (calls the sync resolver — typically
    very few files need full hashing).

    Returns the number of files scanned.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")
    directory = directory.resolve()

    count, scan_date = asyncio.run(_scan_directory_async(directory, conn, concurrency))

    # Phase 2: resolve collisions with full hashes
    resolve_collisions(conn)

    # Record this directory as scanned
    add_scanned_dir(conn, str(directory), scan_date)

    return count
