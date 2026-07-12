# m3dedup

Simple file deduplication scanner. Scans a directory recursively and records file metadata (name, path, size, mtime, MD5 hash) into a SQLite database. Duplicate files are identified by matching MD5 hashes.

## Performance optimisations

- **Partial hashing**: large files (>4 KB) are hashed using only the first and last 4 KB. The full hash is computed only when multiple files share the same partial hash, dramatically reducing I/O on directories with mostly unique files.
- **mtime caching**: on re-scans, files whose mtime hasn't changed are skipped entirely — no file reading or hashing.
- **Async I/O**: the `scan-async` command hashes files concurrently using a thread pool.

## Requirements

- Python 3.10 or newer
- No external dependencies (stdlib only)

## Installation

```bash
cd ~/py/m3dedup
pip install -e .
```

## Usage

Scan a directory (records file metadata into the database):

```bash
python -m m3dedup scan /path/to/directory
```

Scan with async I/O (hashes multiple files concurrently — faster on directories with many files):

```bash
python -m m3dedup scan /path/to/directory --async
python -m m3dedup scan /path/to/directory --async --concurrency 64
```

The `--concurrency` flag is optional. If omitted, it defaults to `min(32, CPU_threads × 4)`.

> `scan-async` is kept as a backwards-compatible alias for `scan --async`.

Re-scan all previously scanned directories:

```bash
python -m m3dedup rescan
python -m m3dedup rescan --async
```

List duplicate file groups (files with identical MD5 hashes):

```bash
python -m m3dedup duplicates
```

By default the database is stored at `~/dedup.db`. You can override this with the `--db` option:

```bash
python -m m3dedup scan /path/to/directory --db /other/path.db
python -m m3dedup duplicates --db /other/path.db
```

## Database Schema

Table: `files`

| Column      | Description                              |
|-------------|------------------------------------------|
| `id`        | Auto-increment primary key               |
| `filename`  | File name without directory              |
| `full_path` | Absolute path to the file (unique)      |
| `scan_date` | UTC timestamp of the scan                |
| `mtime`     | File modification time (UTC ISO format) |
| `size_bytes`| File size in bytes                       |
| `md5_hash`  | MD5 hex digest of file contents          |
| `md5_partial` | Partial MD5 (first+last 4 KB) — NULL for small files where the full hash is used as partial |

## Notes

- Files are hashed in 64 KB chunks, so memory usage stays flat regardless of file size.
- Re-scanning the same directory updates existing entries (upsert) rather than creating duplicates.
- MD5 is used for speed. It is sufficient for deduplication but should not be relied on for security purposes.
