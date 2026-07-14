# dedup

Simple file deduplication scanner. Scans a directory recursively and records file metadata (name, path, size, mtime, MD5 hash) into a SQLite database. Duplicate files are identified by matching MD5 hashes and shown in various reports. Some reports allow user to interactively delete duplicates.

## Performance Optimisations

- **Partial hashing**: large files (>4 KB) are hashed using only the first and last 4 KB. The full hash is computed only when multiple files share the same partial hash, dramatically reducing I/O on directories with mostly unique files.
- **mtime caching**: on re-scans, files whose mtime hasn't changed are skipped entirely — no file reading or hashing.
- **Async I/O**: scanning uses async I/O by default, hashing files concurrently via a thread pool. Use `--sync` for the synchronous scanner.
- **Constant memory**: files are hashed in 64 KB chunks, so memory usage stays flat regardless of file size.
- **MD5 for speed**: MD5 is sufficient for deduplication but should not be relied on for security purposes.

## Requirements

- Python 3.10 or newer
- [rich](https://github.com/Textualize/rich) — for coloured console output and progress bars

## Installation

```bash
cd ~/py/m3dedup
pip install -e .
```

## Usage

Scan a directory (records file metadata into the database — async by default):

```bash
dedup scan /path/to/directory
```

Force synchronous scanning (no concurrent hashing):

```bash
dedup scan /path/to/directory --sync
```

The `--concurrency` flag controls how many files are hashed in parallel. If omitted, it defaults to `min(32, CPU_threads × 4)`.

```bash
dedup scan /path/to/directory --concurrency 64
```

Re-scan all previously scanned directories:

```bash
dedup rescan
dedup rescan --sync
```

List all previously scanned directories:

```
dedup dirs
```

List duplicate file groups (files with identical MD5 hashes):

```bash
dedup show
```

### Show Output Formats

The `show` command accepts an optional integer argument to select the output format:

| Command | Description |
|---------|-------------|
| `dedup show` | Rich console output (default) — colored, with human-readable sizes |
| `dedup show 0` | Same as above |
| `dedup show 1` | Plain text — no colors, pipeable to other tools |
| `dedup show 2` | JSON — machine-readable, suitable for scripts |
| `dedup show 3` | Interactive dedup — prompts you to keep one file per group and delete the rest (with confirmation) |

By default the database is stored at `~/dedup.db`. You can override this with the `--db` option:

```bash
dedup scan /path/to/directory --db /other/path.db
dedup show --db /other/path.db
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

Table: `scanned_dirs`

| Column      | Description                              |
|-------------|------------------------------------------|
| `id`        | Auto-increment primary key               |
| `full_path` | Absolute path to the scanned directory (unique) |
| `scan_date` | UTC timestamp of the scan               |
