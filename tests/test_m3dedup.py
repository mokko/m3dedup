"""Tests for m3dedup — DB, sync scanner, async scanner, and CLI."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from m3dedup.db import (
    find_duplicates,
    find_partial_collision_groups,
    get_cached_file,
    insert_file,
    open_db,
)
from m3dedup.scanner import md5_file, md5_partial_file, scan_directory
from m3dedup.scanner_async import DEFAULT_CONCURRENCY, scan_directory_async
from m3dedup.cli import main as cli_main


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path):
    db = open_db(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def sample_dir(tmp_path):
    """Create a temp directory with known files and duplicates."""
    d = tmp_path / "sample"
    d.mkdir()
    (d / "unique_a.txt").write_bytes(b"alpha")
    (d / "unique_b.txt").write_bytes(b"beta")
    (d / "dup1.txt").write_bytes(b"identical content")
    (d / "dup2.txt").write_bytes(b"identical content")
    sub = d / "sub"
    sub.mkdir()
    (sub / "dup3.txt").write_bytes(b"identical content")
    (sub / "nested.txt").write_bytes(b"nested unique")
    return d


# ── DB tests ──────────────────────────────────────────────────────────

class TestDB:
    def test_open_db_creates_schema(self, tmp_path):
        conn = open_db(tmp_path / "fresh.db")
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert ("files",) in tables
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        assert "md5_partial" in cols
        conn.close()

    def test_open_db_migrates_old_schema(self, tmp_path):
        """Old DB without md5_partial should get migrated."""
        db = tmp_path / "old.db"
        conn = __import__("sqlite3").connect(str(db))
        conn.executescript("""
            CREATE TABLE files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                full_path TEXT NOT NULL UNIQUE,
                scan_date TEXT NOT NULL,
                mtime TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                md5_hash TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO files (filename, full_path, scan_date, mtime, size_bytes, md5_hash) "
            "VALUES ('a.txt', '/a', '2025', '2025', 10, 'abc')"
        )
        conn.commit()
        conn.close()
        # Now open with our open_db — should add md5_partial column
        conn2 = open_db(db)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(files)").fetchall()}
        assert "md5_partial" in cols
        row = conn2.execute("SELECT md5_partial FROM files WHERE full_path='/a'").fetchone()
        assert row[0] is None  # existing rows get NULL
        conn2.close()

    def test_insert_and_retrieve(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "abc123")
        row = conn.execute("SELECT filename, md5_hash, md5_partial FROM files WHERE full_path = '/path/a.txt'").fetchone()
        assert row == ("a.txt", "abc123", None)

    def test_insert_with_partial(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "abc123", md5_partial="def456")
        row = conn.execute("SELECT md5_hash, md5_partial FROM files WHERE full_path = '/path/a.txt'").fetchone()
        assert row == ("abc123", "def456")

    def test_insert_upsert_on_conflict(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "old_hash", md5_partial="old_p")
        insert_file(conn, "a.txt", "/path/a.txt", "2025-02-01T00:00:00+00:00", 200, "new_hash", md5_partial="new_p")
        rows = conn.execute("SELECT md5_hash, size_bytes, md5_partial FROM files WHERE full_path = '/path/a.txt'").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("new_hash", 200, "new_p")

    def test_get_cached_file_exists(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "abc123", md5_partial="def")
        result = get_cached_file(conn, "/path/a.txt")
        assert result == ("2025-01-01T00:00:00+00:00", "abc123", "def")

    def test_get_cached_file_missing(self, conn):
        assert get_cached_file(conn, "/nonexistent") is None

    def test_find_duplicates(self, conn):
        insert_file(conn, "a.txt", "/p/a.txt", "2025-01-01T00:00:00+00:00", 10, "hash1")
        insert_file(conn, "b.txt", "/p/b.txt", "2025-01-01T00:00:00+00:00", 10, "hash1")
        insert_file(conn, "c.txt", "/p/c.txt", "2025-01-01T00:00:00+00:00", 20, "hash2")
        groups = find_duplicates(conn)
        assert len(groups) == 1
        assert len(groups[0]) == 2
        assert {f["filename"] for f in groups[0]} == {"a.txt", "b.txt"}

    def test_find_duplicates_none(self, conn):
        insert_file(conn, "a.txt", "/p/a.txt", "2025-01-01T00:00:00+00:00", 10, "hash1")
        insert_file(conn, "b.txt", "/p/b.txt", "2025-01-01T00:00:00+00:00", 10, "hash2")
        assert find_duplicates(conn) == []


# ── md5_file tests ────────────────────────────────────────────────────

class TestMd5File:
    def test_known_hash(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"hello world"
        f.write_bytes(content)
        expected = hashlib.md5(content).hexdigest()
        assert md5_file(f) == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert md5_file(f) == hashlib.md5(b"").hexdigest()

    def test_large_file_chunked(self, tmp_path):
        f = tmp_path / "large.bin"
        content = b"x" * 200_000
        f.write_bytes(content)
        assert md5_file(f) == hashlib.md5(content).hexdigest()


# ── md5_partial_file tests ────────────────────────────────────────────

class TestMd5PartialFile:
    def test_small_file_full_hash(self, tmp_path):
        """Files <= 4 KB should get a full hash as the partial hash."""
        f = tmp_path / "small.bin"
        content = b"small file"
        f.write_bytes(content)
        partial = md5_partial_file(f, len(content))
        assert partial == hashlib.md5(content).hexdigest()

    def test_large_file_partial_differs_from_full(self, tmp_path):
        """Files > 8 KB should have a partial hash that differs from the full hash."""
        f = tmp_path / "big.bin"
        content = b"A" * 100_000
        f.write_bytes(content)
        partial = md5_partial_file(f, len(content))
        full = md5_file(f)
        assert partial != full

    def test_identical_large_files_same_partial(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        content = b"X" * 100_000
        f1.write_bytes(content)
        f2.write_bytes(content)
        assert md5_partial_file(f1, len(content)) == md5_partial_file(f2, len(content))

    def test_different_large_files_different_partial(self, tmp_path):
        """Files that differ at the start should have different partials."""
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"A" * 100_000)
        f2.write_bytes(b"B" * 100_000)
        assert md5_partial_file(f1, 100_000) != md5_partial_file(f2, 100_000)

    def test_boundary_4kb(self, tmp_path):
        """File of exactly 4 KB should get full hash as partial."""
        f = tmp_path / "exact.bin"
        content = b"X" * 4096
        f.write_bytes(content)
        partial = md5_partial_file(f, len(content))
        assert partial == hashlib.md5(content).hexdigest()

    def test_boundary_4097(self, tmp_path):
        """File of 4097 bytes should get a partial hash (not full)."""
        f = tmp_path / "over.bin"
        content = b"X" * 4097
        f.write_bytes(content)
        partial = md5_partial_file(f, len(content))
        full = md5_file(f)
        assert partial != full


# ── sync scanner tests ────────────────────────────────────────────────

class TestScanDirectory:
    def test_scan_records_all_files(self, sample_dir, conn):
        count = scan_directory(sample_dir, conn)
        assert count == 6

    def test_scan_finds_duplicates(self, sample_dir, conn):
        scan_directory(sample_dir, conn)
        groups = find_duplicates(conn)
        assert len(groups) == 1
        assert len(groups[0]) == 3
        names = {f["filename"] for f in groups[0]}
        assert names == {"dup1.txt", "dup2.txt", "dup3.txt"}

    def test_scan_empty_directory(self, tmp_path, conn):
        (tmp_path / "empty").mkdir()
        assert scan_directory(tmp_path / "empty", conn) == 0

    def test_scan_nonexistent_dir(self, conn):
        with pytest.raises(NotADirectoryError):
            scan_directory("/nonexistent/path", conn)

    def test_rescan_preserves_data(self, sample_dir, conn):
        scan_directory(sample_dir, conn)
        first_hashes = {r[0]: r[1] for r in conn.execute("SELECT full_path, md5_hash FROM files").fetchall()}
        scan_directory(sample_dir, conn)
        second_hashes = {r[0]: r[1] for r in conn.execute("SELECT full_path, md5_hash FROM files").fetchall()}
        assert first_hashes == second_hashes

    def test_rescan_skips_unchanged_mtime(self, sample_dir, conn):
        scan_directory(sample_dir, conn)
        import m3dedup.scanner as scanner_mod
        original = scanner_mod.md5_partial_file
        call_count = 0
        def spy(path, size):
            nonlocal call_count
            call_count += 1
            return original(path, size)
        scanner_mod.md5_partial_file = spy
        try:
            scan_directory(sample_dir, conn)
        finally:
            scanner_mod.md5_partial_file = original
        assert call_count == 0

    def test_rescan_rehashes_modified_file(self, sample_dir, conn):
        scan_directory(sample_dir, conn)
        target = sample_dir / "unique_a.txt"
        time.sleep(0.01)
        target.write_bytes(b"changed content")
        import m3dedup.scanner as scanner_mod
        original = scanner_mod.md5_partial_file
        hashed = []
        def spy(path, size):
            hashed.append(str(path))
            return original(path, size)
        scanner_mod.md5_partial_file = spy
        try:
            scan_directory(sample_dir, conn)
        finally:
            scanner_mod.md5_partial_file = original
        assert len(hashed) == 1
        assert "unique_a.txt" in hashed[0]
        cached = get_cached_file(conn, str(target))
        assert cached[1] == hashlib.md5(b"changed content").hexdigest()

    def test_large_duplicates_resolved(self, tmp_path, conn):
        """Large duplicate files should get full hashes after resolve phase."""
        d = tmp_path / "big_dups"
        d.mkdir()
        content = b"X" * 100_000
        (d / "a.bin").write_bytes(content)
        (d / "b.bin").write_bytes(content)
        scan_directory(d, conn)
        groups = find_duplicates(conn)
        assert len(groups) == 1
        assert len(groups[0]) == 2
        full_hash = hashlib.md5(content).hexdigest()
        assert groups[0][0]["md5_hash"] == full_hash

    def test_partial_stored_for_large_files(self, tmp_path, conn):
        """Large files should have md5_partial stored and different from full."""
        d = tmp_path / "single_big"
        d.mkdir()
        (d / "big.bin").write_bytes(b"Y" * 100_000)
        scan_directory(d, conn)
        row = conn.execute("SELECT md5_hash, md5_partial FROM files WHERE full_path LIKE '%big.bin'").fetchone()
        assert row[0] is not None
        assert row[1] is not None
        assert row[0] != row[1]


# ── async scanner tests ───────────────────────────────────────────────

class TestScanDirectoryAsync:
    def test_scan_records_all_files(self, sample_dir, conn):
        count = scan_directory_async(sample_dir, conn)
        assert count == 6

    def test_scan_finds_duplicates(self, sample_dir, conn):
        scan_directory_async(sample_dir, conn)
        groups = find_duplicates(conn)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_sync_and_async_match(self, sample_dir, tmp_path):
        db1 = open_db(tmp_path / "sync.db")
        scan_directory(sample_dir, db1)
        sync_groups = find_duplicates(db1)
        db1.close()

        db2 = open_db(tmp_path / "async.db")
        scan_directory_async(sample_dir, db2)
        async_groups = find_duplicates(db2)
        db2.close()

        assert len(sync_groups) == len(async_groups)
        for sg, ag in zip(sync_groups, async_groups):
            assert {f["md5_hash"] for f in sg} == {f["md5_hash"] for f in ag}
            assert {f["filename"] for f in sg} == {f["filename"] for f in ag}

    def test_rescan_skips_unchanged_mtime(self, sample_dir, conn):
        scan_directory_async(sample_dir, conn)
        import m3dedup.scanner_async as async_mod
        original = async_mod.md5_partial_file
        call_count = 0
        def spy(path, size):
            nonlocal call_count
            call_count += 1
            return original(path, size)
        async_mod.md5_partial_file = spy
        try:
            scan_directory_async(sample_dir, conn)
        finally:
            async_mod.md5_partial_file = original
        assert call_count == 0

    def test_concurrency_flag(self, sample_dir, conn):
        count = scan_directory_async(sample_dir, conn, concurrency=2)
        assert count == 6

    def test_default_concurrency_is_sensible(self):
        import os
        expected = min(32, (os.cpu_count() or 4) * 4)
        assert DEFAULT_CONCURRENCY == expected
        assert 4 <= DEFAULT_CONCURRENCY <= 32

    def test_large_duplicates_resolved(self, tmp_path, conn):
        """Async scanner should also resolve large duplicates."""
        d = tmp_path / "big_dups"
        d.mkdir()
        content = b"Z" * 100_000
        (d / "a.bin").write_bytes(content)
        (d / "b.bin").write_bytes(content)
        scan_directory_async(d, conn)
        groups = find_duplicates(conn)
        assert len(groups) == 1
        assert len(groups[0]) == 2
        full_hash = hashlib.md5(content).hexdigest()
        assert groups[0][0]["md5_hash"] == full_hash


# ── CLI tests ─────────────────────────────────────────────────────────

class TestCLI:
    def test_scan_command(self, sample_dir, tmp_path, capsys):
        db = tmp_path / "cli.db"
        rc = cli_main(["scan", str(sample_dir), "--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "6 file(s) recorded" in out

    def test_scan_async_command(self, sample_dir, tmp_path, capsys):
        db = tmp_path / "cli_async.db"
        rc = cli_main(["scan-async", str(sample_dir), "--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "6 file(s) recorded" in out

    def test_duplicates_command(self, sample_dir, tmp_path, capsys):
        db = tmp_path / "cli_dupes.db"
        cli_main(["scan", str(sample_dir), "--db", str(db)])
        rc = cli_main(["duplicates", "--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "1 duplicate group(s)" in out
        assert "3 file(s) total" in out

    def test_duplicates_none(self, tmp_path, capsys):
        db = tmp_path / "cli_empty.db"
        open_db(db).close()
        rc = cli_main(["duplicates", "--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No duplicates found" in out

    def test_no_subcommand(self, capsys):
        with pytest.raises(SystemExit):
            cli_main([])

    def test_scan_async_concurrency_flag(self, sample_dir, tmp_path, capsys):
        db = tmp_path / "cli_conc.db"
        rc = cli_main(["scan-async", str(sample_dir), "--db", str(db), "--concurrency", "4"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Concurrency: 4" in out

    def test_duplicates_sorted_by_size_desc(self, tmp_path, capsys):
        d = tmp_path / "sort_demo"
        d.mkdir()
        (d / "s1.txt").write_bytes(b"small duplicate")
        (d / "s2.txt").write_bytes(b"small duplicate")
        big = b"x" * 1_048_576
        (d / "b1.bin").write_bytes(big)
        (d / "b2.bin").write_bytes(big)

        db = tmp_path / "sort.db"
        cli_main(["scan", str(d), "--db", str(db)])
        cli_main(["duplicates", "--db", str(db)])
        out = capsys.readouterr().out

        group1_pos = out.find("Group 1")
        group2_pos = out.find("Group 2")
        assert group1_pos < group2_pos
        assert "1.0 MB" in out[group1_pos:group2_pos]
        assert "bytes" in out[group2_pos:]

    def test_duplicates_human_readable_size(self, tmp_path, capsys):
        d = tmp_path / "size_demo"
        d.mkdir()
        (d / "a.txt").write_bytes(b"identical")
        (d / "b.txt").write_bytes(b"identical")

        db = tmp_path / "size.db"
        cli_main(["scan", str(d), "--db", str(db)])
        cli_main(["duplicates", "--db", str(db)])
        out = capsys.readouterr().out
        assert "9 bytes" in out
        assert "wasted" in out

    def test_duplicates_shows_wasted_total(self, sample_dir, tmp_path, capsys):
        db = tmp_path / "waste.db"
        cli_main(["scan", str(sample_dir), "--db", str(db)])
        cli_main(["duplicates", "--db", str(db)])
        out = capsys.readouterr().out
        assert "wasted" in out
