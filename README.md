# m3dedup

Simple file deduplication scanner. Scans a directory recursively and records file metadata (name, path, size, mtime, MD5 hash) into a SQLite database. Duplicate files are identified by matching MD5 hashes.

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
python -m m3dedup scan-async /path/to/directory
python -m m3dedup scan-async /path/to/directory --concurrency 64
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

## Notes

- Files are hashed in 64 KB chunks, so memory usage stays flat regardless of file size.
- Re-scanning the same directory updates existing entries (upsert) rather than creating duplicates.
- MD5 is used for speed. It is sufficient for deduplication but should not be relied on for security purposes.
