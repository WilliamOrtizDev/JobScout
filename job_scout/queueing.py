from __future__ import annotations

from bisect import bisect_right
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable


def _candidate_key(job: dict[str, Any]) -> str:
    return "\x1f".join(
        str(job.get(field) or "")
        for field in ("source", "tenant", "source_id", "canonical_url")
    )


def _read_cursor(path: Path) -> str:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return str(value.get("last_key") or "") if isinstance(value, dict) else ""


def _write_cursor(path: Path, last_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"last_key": last_key}, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rotating_batch(
    jobs: Iterable[dict[str, Any]],
    *,
    limit: int,
    cursor_path: Path | None = None,
    cursor_store: Any | None = None,
    queue_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return a stable round-robin slice so overflow cannot starve."""
    if limit < 1:
        raise ValueError("batch limit must be positive")
    if (cursor_path is None) == (cursor_store is None):
        raise ValueError("provide exactly one cursor backend")
    if cursor_store is not None and not queue_name:
        raise ValueError("queue_name is required for a SQLite cursor")
    ordered = sorted(jobs, key=_candidate_key)
    if not ordered:
        return []
    keys = [_candidate_key(job) for job in ordered]
    if cursor_store is not None:
        cursor = cursor_store.get_queue_cursor(queue_name)
    else:
        assert cursor_path is not None
        cursor = _read_cursor(Path(cursor_path))
    start = bisect_right(keys, cursor) % len(ordered)
    count = min(limit, len(ordered))
    batch = [ordered[(start + index) % len(ordered)] for index in range(count)]
    last_key = _candidate_key(batch[-1])
    if cursor_store is not None:
        cursor_store.set_queue_cursor(queue_name, last_key)
    else:
        assert cursor_path is not None
        _write_cursor(Path(cursor_path), last_key)
    return batch