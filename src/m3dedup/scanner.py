"""File scanning and hashing logic."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .db import get_cached_file, insert_file

log = logging.getLogger(__name__)

CHUNK_SIZE = 65536  # 64 KB


def md5_file(path: Path) -> str:
    """Return the MD5 hex digest of a file, read in 64 KB chunks."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scan_directory(directory: str | Path, conn) -> int:
    """
    Scan *directory* recursively and insert every file into the database.

    Returns the number of files scanned.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    scan_date = datetime.now(timezone.utc).isoformat()
    count = 0

    for root, _dirs, files in os.walk(directory):
        for name in files:
            full = Path(root) / name
            try:
                stat = full.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()

                cached = get_cached_file(conn, str(full))
                if cached and cached[0] == mtime:
                    md5 = cached[1]
                else:
                    md5 = md5_file(full)

                insert_file(
                    conn,
                    filename=name,
                    full_path=str(full),
                    mtime=mtime,
                    size_bytes=stat.st_size,
                    md5_hash=md5,
                    scan_date=scan_date,
                )
                count += 1
            except (OSError, PermissionError) as exc:
                log.warning("Skipping %s: %s", full, exc)

    conn.commit()
    return count
