"""Rich progress bar helpers shared by sync and async scanners."""

from __future__ import annotations

import os
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


def count_files(directory: Path) -> int:
    """Count all regular files under *directory* (directory listing only, no hashing)."""
    total = 0
    for _root, _dirs, files in os.walk(directory):
        total += len(files)
    return total


def make_progress() -> Progress:
    """Return a configured Rich Progress instance for scan display."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Scanning"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.completed}/{task.total} files"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        transient=False,
    )


def make_resolve_progress() -> Progress:
    """Return a configured Rich Progress instance for collision resolution."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]Resolving collisions"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.completed}/{task.total} files"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        transient=False,
    )
