#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.state_store import SQLiteStateStore


@dataclass(frozen=True)
class LegacySnapshot:
    path: Path
    data: bytes
    sha256: str
    device: int
    inode: int


def _read_legacy_snapshot(path: Path) -> LegacySnapshot:
    source = Path(path)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("legacy state must be a regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    return LegacySnapshot(
        path=source.resolve(),
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _durable_archive(snapshot: LegacySnapshot, database: Path) -> Path:
    archive_dir = Path(database).parent / "legacy-archive"
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise ValueError("legacy archive directory is unsafe")
    os.chmod(archive_dir, 0o700)
    _fsync_directory(archive_dir.parent)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(archive_dir, directory_flags)
    archive_name = f"state.{snapshot.sha256}.json"
    try:
        for existing_name in os.listdir(directory_fd):
            if not (existing_name.startswith("state.") and existing_name.endswith(".json")):
                continue
            try:
                existing_fd = os.open(
                    existing_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except OSError:
                continue
            try:
                if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                    continue
                chunks = []
                while chunk := os.read(existing_fd, 1024 * 1024):
                    chunks.append(chunk)
                if b"".join(chunks) == snapshot.data:
                    return (archive_dir / existing_name).resolve()
            finally:
                os.close(existing_fd)
        try:
            descriptor = os.open(
                archive_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            descriptor = os.open(
                archive_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("legacy archive is unsafe")
                chunks = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                if b"".join(chunks) != snapshot.data:
                    raise ValueError("legacy archive does not match source snapshot")
            finally:
                os.close(descriptor)
        else:
            try:
                view = memoryview(snapshot.data)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return (archive_dir / archive_name).resolve()


def _fsync_directory(path: Path) -> None:
    parent_fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _staged_paths(source: Path) -> list[Path]:
    return sorted(source.parent.glob(f".{source.name}.migration-*.staged"))


def _stage_legacy_state(source: Path) -> Path:
    source = Path(os.path.abspath(source))
    staged = _staged_paths(source)
    if len(staged) > 1:
        raise ValueError("multiple staged legacy states require manual recovery")
    if staged:
        return staged[0]
    if not os.path.lexists(source):
        raise FileNotFoundError(source)
    if not stat.S_ISREG(os.lstat(source).st_mode):
        raise ValueError("legacy state must be a regular file")
    destination = source.with_name(f".{source.name}.migration-{uuid4().hex}.staged")
    os.rename(source, destination)
    _fsync_directory(source.parent)
    return destination


def _restore_staged_state(staged: Path, source: Path) -> None:
    if not os.path.lexists(source) and os.path.lexists(staged):
        os.rename(staged, source)
        _fsync_directory(source.parent)


def _unlink_legacy_snapshot(snapshot: LegacySnapshot) -> None:
    snapshot.path.unlink()
    _fsync_directory(snapshot.path.parent)


def install_legacy_state(database: Path, legacy: Path) -> dict[str, object]:
    store = SQLiteStateStore(database)
    source = Path(os.path.abspath(legacy))
    staged = _stage_legacy_state(source)
    try:
        snapshot = _read_legacy_snapshot(staged)
        archive = _durable_archive(snapshot, database)
        recovered = store.legacy_import_matches(source, snapshot.sha256)
        if recovered:
            imported = None
        else:
            imported = store.import_legacy_json_bytes(snapshot.data, source_path=source)
        _unlink_legacy_snapshot(snapshot)
        if os.path.lexists(source):
            raise ValueError(
                "legacy state replacement appeared during migration; preserved for review"
            )
    except Exception:
        _restore_staged_state(staged, source)
        raise
    result: dict[str, object] = {
        "initialized": True,
        "legacy_archive": str(archive),
        **store.stats(),
    }
    if recovered:
        result["recovered"] = True
    else:
        result["imported"] = imported
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize SQLite state and safely archive one legacy JSON state file"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--legacy", required=True, type=Path)
    args = parser.parse_args()

    if not os.path.lexists(args.legacy) and not _staged_paths(args.legacy.resolve(strict=False)):
        store = SQLiteStateStore(args.database)
        print(json.dumps({"initialized": True, "legacy_archive": None, **store.stats()}))
        return 0
    try:
        result = install_legacy_state(args.database, args.legacy)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
