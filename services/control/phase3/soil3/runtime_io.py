"""
Shared runtime I/O helpers for phase3.

The control loop and nightly auditor run in separate processes, so JSON
read-modify-write operations must be protected by the same sidecar lock file.
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


@contextmanager
def file_lock(path: Path, exclusive: bool = True):
    """Lock a sidecar file shared by all readers/writers of ``path``."""
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file, mode)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _load_json_unlocked(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())
    shutil.move(str(tmp), str(path))


def load_json_locked(path: Path, default: Any) -> Any:
    try:
        with file_lock(path, exclusive=False):
            return _load_json_unlocked(path, default)
    except (json.JSONDecodeError, OSError):
        return default


def save_json_locked(path: Path, data: Any) -> None:
    with file_lock(path, exclusive=True):
        _save_json_unlocked(path, data)


def update_json_locked(
    path: Path,
    default: Any,
    updater: Callable[[Any], Optional[Any]],
) -> Any:
    """Load, mutate and save JSON while holding one exclusive lock."""
    with file_lock(path, exclusive=True):
        try:
            data = _load_json_unlocked(path, default)
        except (json.JSONDecodeError, OSError):
            data = default
        updated = updater(data)
        if updated is not None:
            data = updated
        _save_json_unlocked(path, data)
        return data


def append_csv_row_locked(path: Path, header: Iterable[str], row: Iterable[Any]) -> None:
    with file_lock(path, exclusive=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(list(header))
            writer.writerow(list(row))


def read_csv_dicts_locked(path: Path) -> list[dict[str, str]]:
    with file_lock(path, exclusive=False):
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8", newline="") as f:
            # A partially written edge log may contain isolated NUL bytes.
            # Strip them while reading so one damaged byte cannot stop the
            # safety loop; the source file is left untouched for audit.
            sanitized_lines = (line.replace("\0", "") for line in f)
            return list(csv.DictReader(sanitized_lines))
