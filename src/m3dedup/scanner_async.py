"""Async file scanning and hashing logic.

Uses asyncio to hash multiple files concurrently. File hashing is I/O-bound,
so each file is hashed in a thread via ``asyncio.to_thread``. A semaphore
limits how many files are open at once. SQLite writes happen on the main
thread after each file is hashed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .db import insert_file
from .scanner import CHUNK_SIZE

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 32


def _md5_file(path: Path) -> str:
    """Return the MD5 hex digest of a file, read in 64 KB chunks."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def _scan_one(
    full: Path,
    scan_date: str,
    conn,
    sem: asyncio.Semaphore,
) -> bool:
    """Hash a single file and insert it into the DB. Returns True on success."""
    async with sem:
        try:
            stat = full.stat()
            md5 = await asyncio.to_thread(_md5_file, full)
            insert_file(
                conn,
                filename=full.name,
                full_path=str(full),
                mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                size_bytes=stat.st_size,
                md5_hash=md5,
                scan_date=scan_date,
            )
            return True
        except (OSError, PermissionError) as exc:
            log.warning("Skipping %s: %s", full, exc)
            return False


async def _scan_directory_async(
    directory: Path,
    conn,
    concurrency: int,
) -> int:
    scan_date = datetime.now(timezone.utc).isoformat()
    sem = asyncio.Semaphore(concurrency)
    count = 0

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

    conn.commit()
    return count


def scan_directory_async(
    directory: str | Path,
    conn,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> int:
    """
    Scan *directory* recursively and insert every file into the database
    using async I/O for concurrent hashing.

    Returns the number of files scanned.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    return asyncio.run(_scan_directory_async(directory, conn, concurrency))
