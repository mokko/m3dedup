"""SQLite database schema and operations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    full_path   TEXT    NOT NULL UNIQUE,
    scan_date   TEXT    NOT NULL,
    mtime       TEXT    NOT NULL,
    size_bytes  INTEGER NOT NULL,
    md5_hash    TEXT    NOT NULL,
    md5_partial TEXT
);

CREATE INDEX IF NOT EXISTS idx_md5_hash    ON files(md5_hash);
CREATE INDEX IF NOT EXISTS idx_md5_partial  ON files(md5_partial);
CREATE INDEX IF NOT EXISTS idx_size        ON files(size_bytes);
CREATE UNIQUE INDEX IF NOT EXISTS idx_full_path ON files(full_path);

CREATE TABLE IF NOT EXISTS scanned_dirs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    full_path   TEXT    NOT NULL UNIQUE,
    scan_date   TEXT    NOT NULL
);
"""

# For files smaller than this, partial hash is skipped (just use full hash).
PARTIAL_THRESHOLD = 4096  # 4 KB — one filesystem block


def open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    # Check if the table already exists (upgrading from older schema)
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
    ).fetchone() is not None

    if table_exists:
        # Migration: add md5_partial column if missing
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "md5_partial" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN md5_partial TEXT")
            conn.commit()

    # Now run schema (CREATE TABLE IF NOT EXISTS + indexes) — safe either way
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_cached_file(
    conn: sqlite3.Connection, full_path: str
) -> tuple[str, str, str | None] | None:
    """Return (mtime, md5_hash, md5_partial) for *full_path* if it exists, else None."""
    row = conn.execute(
        "SELECT mtime, md5_hash, md5_partial FROM files WHERE full_path = ?", (full_path,)
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2]


def insert_file(
    conn: sqlite3.Connection,
    filename: str,
    full_path: str,
    mtime: str,
    size_bytes: int,
    md5_hash: str,
    scan_date: str | None = None,
    md5_partial: str | None = None,
) -> None:
    if scan_date is None:
        scan_date = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO files (filename, full_path, scan_date, mtime, size_bytes, md5_hash, md5_partial)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(full_path) DO UPDATE SET
            filename    = excluded.filename,
            scan_date   = excluded.scan_date,
            mtime       = excluded.mtime,
            size_bytes  = excluded.size_bytes,
            md5_hash    = excluded.md5_hash,
            md5_partial = excluded.md5_partial
        """,
        (filename, full_path, scan_date, mtime, size_bytes, md5_hash, md5_partial),
    )


def update_full_hash(
    conn: sqlite3.Connection, full_path: str, md5_hash: str
) -> None:
    """Update the full md5_hash for a file (used during resolve phase)."""
    conn.execute(
        "UPDATE files SET md5_hash = ? WHERE full_path = ?",
        (md5_hash, full_path),
    )


def find_partial_collision_groups(conn: sqlite3.Connection) -> list[list[dict]]:
    """
    Return groups of files that share the same (size_bytes, md5_partial)
    and have more than one file — these need full hash resolution.
    Only includes files where md5_partial IS NOT NULL.
    """
    rows = conn.execute(
        """
        SELECT filename, full_path, scan_date, mtime, size_bytes, md5_hash, md5_partial
        FROM files
        WHERE md5_partial IS NOT NULL
          AND (size_bytes, md5_partial) IN (
              SELECT size_bytes, md5_partial FROM files
              WHERE md5_partial IS NOT NULL
              GROUP BY size_bytes, md5_partial
              HAVING COUNT(*) > 1
          )
        ORDER BY size_bytes DESC, md5_partial, full_path
        """
    ).fetchall()

    groups: dict[str, list[dict]] = {}
    for row in rows:
        filename, full_path, scan_date, mtime, size_bytes, md5_hash, md5_partial = row
        key = f"{size_bytes}:{md5_partial}"
        groups.setdefault(key, []).append(
            {
                "filename": filename,
                "full_path": full_path,
                "scan_date": scan_date,
                "mtime": mtime,
                "size_bytes": size_bytes,
                "md5_hash": md5_hash,
                "md5_partial": md5_partial,
            }
        )
    return list(groups.values())


def find_duplicates(conn: sqlite3.Connection) -> list[list[dict]]:
    """Return groups of files sharing the same MD5 hash (size > 1)."""
    rows = conn.execute(
        """
        SELECT filename, full_path, scan_date, mtime, size_bytes, md5_hash
        FROM files
        WHERE md5_hash != ''
          AND md5_hash IN (
            SELECT md5_hash FROM files WHERE md5_hash != '' GROUP BY md5_hash HAVING COUNT(*) > 1
        )
        ORDER BY md5_hash, full_path
        """
    ).fetchall()

    groups: dict[str, list[dict]] = {}
    for row in rows:
        filename, full_path, scan_date, mtime, size_bytes, md5_hash = row
        groups.setdefault(md5_hash, []).append(
            {
                "filename": filename,
                "full_path": full_path,
                "scan_date": scan_date,
                "mtime": mtime,
                "size_bytes": size_bytes,
                "md5_hash": md5_hash,
            }
        )
    return list(groups.values())


def delete_file(conn: sqlite3.Connection, full_path: str) -> None:
    """Delete a file entry from the database by full_path."""
    conn.execute("DELETE FROM files WHERE full_path = ?", (full_path,))


def add_scanned_dir(
    conn: sqlite3.Connection, full_path: str, scan_date: str | None = None
) -> None:
    """Record a directory as scanned (upsert by full_path)."""
    if scan_date is None:
        scan_date = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO scanned_dirs (full_path, scan_date)
        VALUES (?, ?)
        ON CONFLICT(full_path) DO UPDATE SET
            scan_date = excluded.scan_date
        """,
        (full_path, scan_date),
    )
    conn.commit()


def get_scanned_dirs(conn: sqlite3.Connection) -> list[dict]:
    """Return all scanned directories, ordered by most recent scan first."""
    rows = conn.execute(
        "SELECT full_path, scan_date FROM scanned_dirs ORDER BY scan_date DESC"
    ).fetchall()
    return [{"full_path": r[0], "scan_date": r[1]} for r in rows]
