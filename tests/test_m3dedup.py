"""Tests for m3dedup — DB, sync scanner, async scanner, and CLI."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from m3dedup.db import find_duplicates, get_cached_file, insert_file, open_db
from m3dedup.scanner import md5_file, scan_directory
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
        conn.close()

    def test_insert_and_retrieve(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "abc123")
        row = conn.execute("SELECT filename, md5_hash FROM files WHERE full_path = '/path/a.txt'").fetchone()
        assert row == ("a.txt", "abc123")

    def test_insert_upsert_on_conflict(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "old_hash")
        insert_file(conn, "a.txt", "/path/a.txt", "2025-02-01T00:00:00+00:00", 200, "new_hash")
        rows = conn.execute("SELECT md5_hash, size_bytes FROM files WHERE full_path = '/path/a.txt'").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("new_hash", 200)

    def test_get_cached_file_exists(self, conn):
        insert_file(conn, "a.txt", "/path/a.txt", "2025-01-01T00:00:00+00:00", 100, "abc123")
        result = get_cached_file(conn, "/path/a.txt")
        assert result == ("2025-01-01T00:00:00+00:00", "abc123")

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
        """File bigger than CHUNK_SIZE to test chunked reading."""
        f = tmp_path / "large.bin"
        content = b"x" * 200_000  # ~3 chunks at 64 KB
        f.write_bytes(content)
        assert md5_file(f) == hashlib.md5(content).hexdigest()


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
        # Spy on md5_file to count actual hash computations
        import m3dedup.scanner as scanner_mod
        original_md5 = scanner_mod.md5_file
        call_count = 0
        def spy(path):
            nonlocal call_count
            call_count += 1
            return original_md5(path)
        scanner_mod.md5_file = spy
        try:
            scan_directory(sample_dir, conn)
        finally:
            scanner_mod.md5_file = original_md5
        # All 6 files have unchanged mtime → 0 hash calls
        assert call_count == 0

    def test_rescan_rehashes_modified_file(self, sample_dir, conn):
        scan_directory(sample_dir, conn)
        # Modify one file
        target = sample_dir / "unique_a.txt"
        time.sleep(0.01)  # ensure mtime changes
        target.write_bytes(b"changed content")
        import m3dedup.scanner as scanner_mod
        original_md5 = scanner_mod.md5_file
        hashed_paths: list[str] = []
        def spy(path):
            hashed_paths.append(str(path))
            return original_md5(path)
        scanner_mod.md5_file = spy
        try:
            scan_directory(sample_dir, conn)
        finally:
            scanner_mod.md5_file = original_md5
        # Only the modified file should be re-hashed
        assert len(hashed_paths) == 1
        assert "unique_a.txt" in hashed_paths[0]
        # Verify the hash was updated
        cached = get_cached_file(conn, str(target))
        assert cached[1] == hashlib.md5(b"changed content").hexdigest()


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
        """Both scanners should produce identical results."""
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
        original = async_mod._md5_file
        call_count = 0
        def spy(path):
            nonlocal call_count
            call_count += 1
            return original(path)
        async_mod._md5_file = spy
        try:
            scan_directory_async(sample_dir, conn)
        finally:
            async_mod._md5_file = original
        assert call_count == 0

    def test_concurrency_flag(self, sample_dir, conn):
        count = scan_directory_async(sample_dir, conn, concurrency=2)
        assert count == 6

    def test_default_concurrency_is_sensible(self):
        """Default should be based on CPU count and capped at 32."""
        import os
        expected = min(32, (os.cpu_count() or 4) * 4)
        assert DEFAULT_CONCURRENCY == expected
        assert 4 <= DEFAULT_CONCURRENCY <= 32


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
