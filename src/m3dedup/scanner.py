"""File scanning and hashing logic.

Two-phase approach for speed:
  1. Partial hash: hash only the first + last 4 KB of large files.
     For files <= PARTIAL_THRESHOLD, the full hash IS the partial hash.
  2. Resolve: for files sharing the same (size, partial hash), compute
     the full MD5 to confirm whether they are true duplicates.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .db import (
    PARTIAL_THRESHOLD,
    add_scanned_dir,
    find_partial_collision_groups,
    get_cached_file,
    insert_file,
    update_full_hash,
)
from .progress import count_files, make_progress, make_resolve_progress

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


def _compute_hashes(full: Path, stat, conn) -> tuple[str, str, str]:
    """
    Core hash resolution logic (synchronous).

    Returns (mtime, partial_hash, full_hash). The full_hash may be ""
    if it needs to be resolved later in the collision phase.

    Cases:
      1. mtime unchanged and partial cached → reuse both
      2. mtime unchanged but no partial (old DB) → compute partial, reuse full
      3. New or modified file → compute partial; full = partial for small
         files, "" for large files (resolved in phase 2)
    """
    mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    cached = get_cached_file(conn, str(full))

    if cached and cached[0] == mtime and cached[2] is not None:
        # Case 1: reuse both cached hashes
        return mtime, cached[2], cached[1]
    elif cached and cached[0] == mtime:
        # Case 2: old DB without partial — compute partial, reuse full
        partial = md5_partial_file(full, stat.st_size)
        return mtime, partial, cached[1]
    else:
        # Case 3: new or modified file
        partial = md5_partial_file(full, stat.st_size)
        if stat.st_size <= PARTIAL_THRESHOLD:
            full_hash = partial
        else:
            full_hash = ""  # will be resolved in phase 2
        return mtime, partial, full_hash


def resolve_hashes(full: Path, stat, conn) -> tuple[str, str]:
    """Synchronous wrapper: returns (partial_hash, full_hash)."""
    _, partial, full_hash = _compute_hashes(full, stat, conn)
    return partial, full_hash


async def resolve_hashes_async(full: Path, stat, conn) -> tuple[str, str]:
    """Async wrapper: offloads hashing to a thread, DB lookup stays on main thread."""
    mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    cached = get_cached_file(conn, str(full))

    if cached and cached[0] == mtime and cached[2] is not None:
        # Case 1: reuse both cached hashes — no I/O needed
        return cached[2], cached[1]
    elif cached and cached[0] == mtime:
        # Case 2: old DB without partial — compute partial in thread
        partial = await asyncio.to_thread(md5_partial_file, full, stat.st_size)
        return partial, cached[1]
    else:
        # Case 3: new or modified file — compute partial in thread
        partial = await asyncio.to_thread(md5_partial_file, full, stat.st_size)
        if stat.st_size <= PARTIAL_THRESHOLD:
            full_hash = partial
        else:
            full_hash = ""
        return partial, full_hash


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
    directory = directory.resolve()

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
                    partial, full_hash = resolve_hashes(full, stat, conn)

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

    # Record this directory as scanned
    add_scanned_dir(conn, str(directory), scan_date)

    return count


def resolve_collisions(conn, progress=None, task_id=None) -> int:
    """
    For files sharing the same (size_bytes, md5_partial), compute the
    full MD5 hash to confirm true duplicates. Files that were already
    fully hashed (small files) are skipped.

    Returns the number of full hashes computed.
    """
    groups = find_partial_collision_groups(conn)

    # Count files that need full hashing
    files_to_resolve = []
    for group in groups:
        for f in group:
            if not f["md5_hash"]:
                files_to_resolve.append(f)

    resolved = 0

    with make_resolve_progress() as resolve_progress:
        task = resolve_progress.add_task("resolve", total=len(files_to_resolve))

        for f in files_to_resolve:
            path = Path(f["full_path"])
            try:
                full_hash = md5_file(path)
                update_full_hash(conn, f["full_path"], full_hash)
                resolved += 1
            except (OSError, PermissionError) as exc:
                log.warning("Skipping %s: %s", path, exc)
            finally:
                resolve_progress.advance(task)

    conn.commit()
    return resolved
