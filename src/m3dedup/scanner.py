"""File scanning and hashing logic.

Two-phase approach for speed:
  1. Partial hash: hash only the first + last 4 KB of large files.
     For files <= PARTIAL_THRESHOLD, the full hash IS the partial hash.
  2. Resolve: for files sharing the same (size, partial hash), compute
     the full MD5 to confirm whether they are true duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .db import (
    PARTIAL_THRESHOLD,
    find_partial_collision_groups,
    get_cached_file,
    insert_file,
    update_full_hash,
)
from .progress import count_files, make_progress

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


def md5_partial_file(path: Path, size: int) -> str:
    """
    Return a partial MD5 hash of a file.

    For files <= PARTIAL_THRESHOLD bytes, the full file is hashed.
    For larger files, the first and last PARTIAL_THRESHOLD bytes are hashed.
    """
    if size <= PARTIAL_THRESHOLD:
        return md5_file(path)

    h = hashlib.md5()
    with open(path, "rb") as f:
        # First PARTIAL_THRESHOLD bytes
        h.update(f.read(PARTIAL_THRESHOLD))
        # Last PARTIAL_THRESHOLD bytes
        if size > PARTIAL_THRESHOLD * 2:
            f.seek(size - PARTIAL_THRESHOLD)
            h.update(f.read(PARTIAL_THRESHOLD))
    return h.hexdigest()


def scan_directory(directory: str | Path, conn) -> int:
    """
    Scan *directory* recursively and insert every file into the database.

    Phase 1: compute partial hashes (fast — only reads 8 KB per large file).
    Phase 2: resolve collisions by computing full hashes only for
    files that share a (size, partial hash) with another file.

    Returns the number of files scanned.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    scan_date = datetime.now(timezone.utc).isoformat()
    total = count_files(directory)
    count = 0

    # ── Phase 1: partial hashing ─────────────────────────────────────
    with make_progress() as progress:
        task = progress.add_task("scan", total=total)

        for root, _dirs, files in os.walk(directory):
            for name in files:
                full = Path(root) / name
                try:
                    stat = full.stat()
                    mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()

                    cached = get_cached_file(conn, str(full))
                    if cached and cached[0] == mtime and cached[2] is not None:
                        # mtime unchanged and we have a partial hash — reuse both
                        partial = cached[2]
                        full_hash = cached[1]
                    elif cached and cached[0] == mtime:
                        # mtime unchanged but no partial hash (old DB) — compute partial
                        partial = md5_partial_file(full, stat.st_size)
                        full_hash = cached[1]
                    else:
                        # File is new or modified — compute partial hash
                        partial = md5_partial_file(full, stat.st_size)
                        # For small files, partial == full hash
                        if stat.st_size <= PARTIAL_THRESHOLD:
                            full_hash = partial
                        else:
                            full_hash = ""  # will be resolved in phase 2

                    insert_file(
                        conn,
                        filename=name,
                        full_path=str(full),
                        mtime=mtime,
                        size_bytes=stat.st_size,
                        md5_hash=full_hash,
                        scan_date=scan_date,
                        md5_partial=partial,
                    )
                    count += 1
                except (OSError, PermissionError) as exc:
                    log.warning("Skipping %s: %s", full, exc)
                finally:
                    progress.advance(task)

    conn.commit()

    # ── Phase 2: resolve collisions with full hashes ─────────────────
    resolve_collisions(conn)

    return count


def resolve_collisions(conn) -> int:
    """
    For files sharing the same (size_bytes, md5_partial), compute the
    full MD5 hash to confirm true duplicates. Files that were already
    fully hashed (small files) are skipped.

    Returns the number of full hashes computed.
    """
    groups = find_partial_collision_groups(conn)
    resolved = 0

    for group in groups:
        for f in group:
            # Skip files that already have a full hash
            # (small files where partial == full, or cached from prior resolve)
            if f["md5_hash"] and f["md5_hash"] != "":
                # Verify: if full hash is set and equals partial, it might
                # still need a real full hash for large files
                if f["size_bytes"] <= PARTIAL_THRESHOLD:
                    continue
                # If we already have a non-empty full hash, skip
                continue

            path = Path(f["full_path"])
            try:
                full_hash = md5_file(path)
                update_full_hash(conn, f["full_path"], full_hash)
                resolved += 1
            except (OSError, PermissionError) as exc:
                log.warning("Skipping %s: %s", path, exc)

    return resolved
