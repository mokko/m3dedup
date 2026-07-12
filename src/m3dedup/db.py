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
    md5_hash    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_md5_hash   ON files(md5_hash);
CREATE INDEX IF NOT EXISTS idx_size       ON files(size_bytes);
CREATE UNIQUE INDEX IF NOT EXISTS idx_full_path ON files(full_path);
"""


def open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_cached_file(
    conn: sqlite3.Connection, full_path: str
) -> tuple[str, str] | None:
    """Return (mtime, md5_hash) for *full_path* if it exists in the DB, else None."""
    row = conn.execute(
        "SELECT mtime, md5_hash FROM files WHERE full_path = ?", (full_path,)
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def insert_file(
    conn: sqlite3.Connection,
    filename: str,
    full_path: str,
    mtime: str,
    size_bytes: int,
    md5_hash: str,
    scan_date: str | None = None,
) -> None:
    if scan_date is None:
        scan_date = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO files (filename, full_path, scan_date, mtime, size_bytes, md5_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(full_path) DO UPDATE SET
            filename   = excluded.filename,
            scan_date  = excluded.scan_date,
            mtime      = excluded.mtime,
            size_bytes = excluded.size_bytes,
            md5_hash   = excluded.md5_hash
        """,
        (filename, full_path, scan_date, mtime, size_bytes, md5_hash),
    )
    conn.commit()


def find_duplicates(conn: sqlite3.Connection) -> list[list[dict]]:
    """Return groups of files sharing the same MD5 hash (size > 1)."""
    rows = conn.execute(
        """
        SELECT filename, full_path, scan_date, mtime, size_bytes, md5_hash
        FROM files
        WHERE md5_hash IN (
            SELECT md5_hash FROM files GROUP BY md5_hash HAVING COUNT(*) > 1
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
