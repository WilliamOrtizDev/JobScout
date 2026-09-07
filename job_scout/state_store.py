from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 4
VALID_APPLICATION_STATUSES = {
    "discovered",
    "verification_pending",
    "verified",
    "rejected",
    "packet_ready",
    "pending_approval",
    "approved",
    "blocked",
    "submitted",
    "skipped",
    "closed",
}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json_text(value).encode()).hexdigest()


def _confined_packet_dir(packet_dir: Path, applications_root: Path) -> Path:
    root = Path(os.path.abspath(applications_root))
    packet = Path(os.path.abspath(packet_dir))
    try:
        relative = packet.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"packet directory is outside applications root: {packet}") from exc
    current = root
    if current.is_symlink():
        raise ValueError(f"applications root must not be a symlink: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"packet path contains a symlink: {current}")
    root_resolved = root.resolve()
    packet_resolved = packet.resolve()
    try:
        packet_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"packet directory is outside applications root: {packet_resolved}"
        ) from exc
    if not packet_resolved.is_dir():
        raise ValueError(f"packet directory does not exist: {packet_resolved}")
    return packet_resolved


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"packet contains an unsafe file: {path}")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def packet_snapshot(
    packet_dir: Path, *, applications_root: Path
) -> tuple[str, dict[str, bytes]]:
    packet = _confined_packet_dir(packet_dir, applications_root)
    required = ("job.json", "resume.pdf", "cover-letter.pdf")
    digest = hashlib.sha256()
    approval_files = (
        "job.json",
        "job-description.md",
        "resume.tex",
        "resume.pdf",
        "cover-letter.tex",
        "cover-letter.pdf",
        "application-answers.md",
        "approval.md",
    )
    snapshots: dict[str, bytes] = {}
    for name in approval_files:
        path = packet / name
        if not os.path.lexists(path):
            continue
        try:
            data = _read_regular_file(path)
        except OSError as exc:
            raise ValueError(f"packet contains an unsafe file: {path}") from exc
        relative = path.relative_to(packet).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        snapshots[name] = data
    for name in required:
        if name not in snapshots:
            raise ValueError(f"packet is missing a regular {name}: {packet / name}")
    return digest.hexdigest(), snapshots


def packet_fingerprint(packet_dir: Path, *, applications_root: Path) -> str:
    return packet_snapshot(packet_dir, applications_root=applications_root)[0]


def materialize_delivery_snapshot(
    snapshot: dict[str, bytes], *, packet_fingerprint: str, applications_root: Path
) -> dict[str, Path]:
    """Durably persist exact approved attachments at private content-addressed paths."""
    if len(packet_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in packet_fingerprint
    ):
        raise ValueError("packet fingerprint must be a lowercase SHA-256 digest")
    attachments = {
        name: snapshot[name] for name in ("resume.pdf", "cover-letter.pdf")
    }
    root = Path(os.path.abspath(applications_root))
    if root.is_symlink():
        raise ValueError(f"applications root must not be a symlink: {root}")
    root = root.resolve(strict=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, directory_flags)
    snapshots_fd = -1
    revision_fd = -1
    try:
        try:
            os.mkdir(".delivery-snapshots", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        snapshots_fd = os.open(".delivery-snapshots", directory_flags, dir_fd=root_fd)
        if not stat.S_ISDIR(os.fstat(snapshots_fd).st_mode):
            raise ValueError("delivery snapshot root is unsafe")
        os.fchmod(snapshots_fd, 0o700)
        try:
            os.mkdir(packet_fingerprint, 0o700, dir_fd=snapshots_fd)
        except FileExistsError:
            pass
        revision_fd = os.open(packet_fingerprint, directory_flags, dir_fd=snapshots_fd)
        if not stat.S_ISDIR(os.fstat(revision_fd).st_mode):
            raise ValueError("delivery snapshot revision is unsafe")
        for name, expected in attachments.items():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=revision_fd)
            except FileExistsError:
                descriptor = os.open(
                    name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=revision_fd
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise ValueError(f"delivery snapshot contains unsafe file: {name}")
                    chunks = []
                    while chunk := os.read(descriptor, 1024 * 1024):
                        chunks.append(chunk)
                    if b"".join(chunks) != expected:
                        raise ValueError(f"delivery snapshot bytes do not match: {name}")
                finally:
                    os.close(descriptor)
            else:
                try:
                    view = memoryview(expected)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                    os.fchmod(descriptor, 0o400)
                finally:
                    os.close(descriptor)
        os.fsync(revision_fd)
        os.fchmod(revision_fd, 0o500)
        os.fsync(snapshots_fd)
        os.fsync(root_fd)
    finally:
        if revision_fd >= 0:
            os.close(revision_fd)
        if snapshots_fd >= 0:
            os.close(snapshots_fd)
        os.close(root_fd)
    revision = root / ".delivery-snapshots" / packet_fingerprint
    return {name: revision / name for name in attachments}


class SQLiteStateStore:
    """Transactional state for collection, application lifecycle, and delivery audit."""

    def __init__(self, path: Path, *, applications_root: Path | None = None):
        self.path = Path(path)
        self.applications_root = Path(
            applications_root
            if applications_root is not None
            else self.path.parent / "applications"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 1:
                try:
                    connection.executescript(
                        """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS application_records (
                        job_id TEXT PRIMARY KEY,
                        canonical_url TEXT,
                        application_url TEXT,
                        content_hash TEXT,
                        status TEXT,
                        payload_json TEXT NOT NULL,
                        imported_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS application_records_canonical_url_idx
                        ON application_records(canonical_url);
                    CREATE INDEX IF NOT EXISTS application_records_application_url_idx
                        ON application_records(application_url);
                    CREATE TABLE IF NOT EXISTS collector_runs (
                        run_id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        accepted_count INTEGER NOT NULL,
                        configured_sources INTEGER NOT NULL,
                        successful_sources INTEGER NOT NULL,
                        source_failures_json TEXT NOT NULL,
                        rejected_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS candidates (
                        source TEXT NOT NULL,
                        tenant TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        application_url TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        last_run_id TEXT NOT NULL REFERENCES collector_runs(run_id),
                        PRIMARY KEY (source, tenant, source_id)
                    );
                    CREATE INDEX IF NOT EXISTS candidates_canonical_url_idx ON candidates(canonical_url);
                    CREATE INDEX IF NOT EXISTS candidates_application_url_idx ON candidates(application_url);
                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                    )
                except Exception:
                    connection.rollback()
                    raise
            if version < 2:
                try:
                    connection.executescript(
                        """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS application_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES application_records(job_id) ON DELETE CASCADE,
                        event_key TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT,
                        event_json TEXT NOT NULL,
                        UNIQUE(job_id, event_key)
                    );
                    CREATE INDEX IF NOT EXISTS application_events_job_time_idx
                        ON application_events(job_id, occurred_at, event_id);
                    CREATE TABLE IF NOT EXISTS delivery_outbox (
                        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES application_records(job_id) ON DELETE CASCADE,
                        fingerprint TEXT NOT NULL,
                        transport TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        delivered_at TEXT,
                        last_error TEXT,
                        UNIQUE(job_id, fingerprint, transport)
                    );
                    CREATE INDEX IF NOT EXISTS delivery_outbox_status_idx
                        ON delivery_outbox(status, created_at, outbox_id);
                    CREATE TABLE IF NOT EXISTS queue_cursors (
                        queue_name TEXT PRIMARY KEY,
                        last_key TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                    )
                except Exception:
                    connection.rollback()
                    raise
            if version < 3:
                try:
                    connection.executescript(
                        """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS verification_attempts (
                        verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES application_records(job_id) ON DELETE CASCADE,
                        attempted_at TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        source_url TEXT,
                        evidence_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS verification_attempts_job_time_idx
                        ON verification_attempts(job_id, attempted_at, verification_id);
                    CREATE TABLE IF NOT EXISTS approvals (
                        approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES application_records(job_id) ON DELETE CASCADE,
                        packet_fingerprint TEXT NOT NULL,
                        application_url TEXT NOT NULL,
                        approved_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        invalidated_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS approvals_job_status_idx
                        ON approvals(job_id, status, approved_at);
                    CREATE TABLE IF NOT EXISTS submission_attempts (
                        submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES application_records(job_id) ON DELETE CASCADE,
                        approval_id INTEGER NOT NULL REFERENCES approvals(approval_id),
                        attempted_at TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        confirmation_json TEXT,
                        error TEXT
                    );
                    CREATE INDEX IF NOT EXISTS submission_attempts_job_time_idx
                        ON submission_attempts(job_id, attempted_at, submission_id);
                    PRAGMA user_version = 3;
                    COMMIT;
                    """
                    )
                except Exception:
                    connection.rollback()
                    raise
            if version < 4:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(delivery_outbox)")
                    }
                    additions = {
                        "packet_fingerprint": (
                            "ALTER TABLE delivery_outbox ADD COLUMN packet_fingerprint TEXT"
                        ),
                        "application_url": (
                            "ALTER TABLE delivery_outbox ADD COLUMN application_url TEXT"
                        ),
                        "claim_token": "ALTER TABLE delivery_outbox ADD COLUMN claim_token TEXT",
                        "claimed_at": "ALTER TABLE delivery_outbox ADD COLUMN claimed_at TEXT",
                        "lease_expires_at": (
                            "ALTER TABLE delivery_outbox ADD COLUMN lease_expires_at TEXT"
                        ),
                    }
                    for name, statement in additions.items():
                        if name not in columns:
                            connection.execute(statement)
                    approval_columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(approvals)")
                    }
                    if "consumed_at" not in approval_columns:
                        connection.execute("ALTER TABLE approvals ADD COLUMN consumed_at TEXT")
                    revision_rows = connection.execute(
                        """
                        SELECT application_records.job_id,
                               application_records.application_url,
                               application_records.payload_json
                        FROM application_records
                        WHERE application_records.status IN (
                            'pending_approval', 'approved', 'blocked'
                        )
                          AND NOT EXISTS (
                              SELECT 1 FROM submission_attempts
                              WHERE submission_attempts.job_id = application_records.job_id
                                AND submission_attempts.outcome = 'submitted'
                          )
                        """
                    ).fetchall()
                    current_revisions: list[tuple[str, str, str, str, tuple[str, ...]]] = []
                    for revision_row in revision_rows:
                        record = json.loads(revision_row["payload_json"])
                        if not isinstance(record, dict):
                            raise RuntimeError(
                                f"application payload is not an object: {revision_row['job_id']}"
                            )
                        application_url = revision_row["application_url"]
                        if not isinstance(application_url, str) or not application_url:
                            raise RuntimeError(
                                f"cannot migrate approval outbox without application URL: "
                                f"{revision_row['job_id']}"
                            )
                        try:
                            canonical_packet = _confined_packet_dir(
                                Path(str(record.get("packet_dir") or "")),
                                applications_root=self.applications_root,
                            )
                            packet_hash, snapshot = packet_snapshot(
                                canonical_packet, applications_root=self.applications_root
                            )
                            packet_job = json.loads(snapshot["job.json"])
                        except (OSError, ValueError) as exc:
                            raise RuntimeError(
                                f"cannot migrate approval packet revision: "
                                f"{revision_row['job_id']}"
                            ) from exc
                        if (
                            not isinstance(packet_job, dict)
                            or packet_job.get("application_url") != application_url
                        ):
                            raise RuntimeError(
                                f"job.json application URL does not match SQLite: "
                                f"{revision_row['job_id']}"
                            )
                        record["job_id"] = str(revision_row["job_id"])
                        record["packet_fingerprint"] = packet_hash
                        record["packet_dir"] = str(canonical_packet)
                        connection.execute(
                            "UPDATE application_records SET payload_json = ? WHERE job_id = ?",
                            (_json_text(record), revision_row["job_id"]),
                        )
                        transports = tuple(
                            str(row["transport"])
                            for row in connection.execute(
                                "SELECT DISTINCT transport FROM delivery_outbox "
                                "WHERE job_id = ? ORDER BY transport",
                                (revision_row["job_id"],),
                            ).fetchall()
                        ) or ("photon",)
                        current_revisions.append(
                            (
                                str(revision_row["job_id"]),
                                str(canonical_packet),
                                packet_hash,
                                application_url,
                                transports,
                            )
                        )
                    connection.execute(
                        """
                        UPDATE approvals
                        SET status = 'invalidated',
                            invalidated_at = COALESCE(invalidated_at, approved_at)
                        WHERE status = 'active'
                        """
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO application_events (
                            job_id, event_key, occurred_at, status, reason, event_json
                        )
                        SELECT application_records.job_id,
                               'schema-v4-reapproval-reset',
                               strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                               'pending_approval',
                               'Schema v4 invalidated legacy approval proof; fresh notification and approval required',
                               json_object(
                                   'at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                                   'status', 'pending_approval',
                                   'reason', 'Schema v4 invalidated legacy approval proof; fresh notification and approval required'
                               )
                        FROM application_records
                        WHERE status IN ('approved', 'blocked')
                          AND NOT EXISTS (
                              SELECT 1 FROM submission_attempts
                              WHERE submission_attempts.job_id = application_records.job_id
                                AND submission_attempts.outcome = 'submitted'
                          )
                        """
                    )
                    connection.execute(
                        """
                        UPDATE application_records
                        SET status = CASE
                                WHEN status = 'submitted' OR EXISTS (
                                    SELECT 1 FROM submission_attempts
                                    WHERE submission_attempts.job_id = application_records.job_id
                                      AND submission_attempts.outcome = 'submitted'
                                ) THEN 'submitted'
                                ELSE 'pending_approval'
                            END,
                            payload_json = json_set(
                                payload_json,
                                '$.status',
                                CASE
                                    WHEN status = 'submitted' OR EXISTS (
                                        SELECT 1 FROM submission_attempts
                                        WHERE submission_attempts.job_id = application_records.job_id
                                          AND submission_attempts.outcome = 'submitted'
                                    ) THEN 'submitted'
                                    ELSE 'pending_approval'
                                END
                            )
                        WHERE status IN ('approved', 'blocked', 'submitted')
                        """
                    )
                    migration_time = datetime.now(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    )
                    for job_id, packet_dir, packet_hash, application_url, transports in current_revisions:
                        connection.execute(
                            "UPDATE delivery_outbox SET status = 'invalidated', "
                            "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                            "WHERE job_id = ? AND status IN "
                            "('pending', 'claimed', 'dispatched', 'delivered')",
                            (job_id,),
                        )
                        for transport in transports:
                            outbox_payload = {
                                "job_id": job_id,
                                "packet_dir": packet_dir,
                                "packet_fingerprint": packet_hash,
                                "application_url": application_url,
                            }
                            fingerprint = _fingerprint(
                                {
                                    "schema": 4,
                                    "transport": transport,
                                    "payload": outbox_payload,
                                }
                            )
                            connection.execute(
                                "INSERT INTO delivery_outbox ("
                                "job_id, fingerprint, transport, payload_json, status, "
                                "attempt_count, created_at, updated_at, delivered_at, "
                                "last_error, packet_fingerprint, application_url, "
                                "claim_token, claimed_at, lease_expires_at"
                                ") VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, ?, ?, "
                                "NULL, NULL, NULL)",
                                (
                                    job_id,
                                    fingerprint,
                                    transport,
                                    _json_text(outbox_payload),
                                    migration_time,
                                    migration_time,
                                    packet_hash,
                                    application_url,
                                ),
                            )
                    connection.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_active_per_job_idx "
                        "ON approvals(job_id) WHERE status = 'active'"
                    )
                    connection.execute(
                        """
                        CREATE TRIGGER IF NOT EXISTS submission_attempts_one_success_per_approval
                        BEFORE INSERT ON submission_attempts
                        WHEN NEW.outcome = 'submitted'
                         AND EXISTS (
                             SELECT 1 FROM submission_attempts
                             WHERE approval_id = NEW.approval_id
                               AND outcome = 'submitted'
                         )
                        BEGIN
                            SELECT RAISE(ABORT, 'approval already has a successful submission');
                        END
                        """
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS delivery_outbox_claim_idx "
                        "ON delivery_outbox(status, lease_expires_at, outbox_id)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS migration_log ("
                        "migration_key TEXT PRIMARY KEY, imported_at TEXT NOT NULL, "
                        "source_path TEXT NOT NULL, source_sha256 TEXT)"
                    )
                    migration_log_columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(migration_log)")
                    }
                    if "source_sha256" not in migration_log_columns:
                        connection.execute(
                            "ALTER TABLE migration_log ADD COLUMN source_sha256 TEXT"
                        )
                    connection.execute("PRAGMA user_version = 4")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @contextmanager
    def run_lock(self):
        lock_path = self.path.with_name(f"{self.path.name}.run.lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"run lock is not a regular file: {lock_path}")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    def import_legacy_json(self, path: Path) -> int:
        source_path = Path(path)
        source_bytes = _read_regular_file(source_path)
        return self.import_legacy_json_bytes(source_bytes, source_path=source_path)

    def import_legacy_json_bytes(self, source_bytes: bytes, *, source_path: Path) -> int:
        source_path = Path(source_path)
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        payload = json.loads(source_bytes)
        jobs = payload.get("jobs", {})
        if not isinstance(jobs, dict):
            raise ValueError("legacy state jobs must be an object keyed by stable job IDs")
        records = jobs.items()
        imported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows: list[tuple[Any, ...]] = []
        event_rows: list[tuple[Any, ...]] = []
        for job_id, record in records:
            if not isinstance(record, dict):
                raise ValueError(f"legacy state record {job_id!r} must be an object")
            stable_id = str(job_id)
            events = record.get("events", [])
            if not isinstance(events, list):
                raise ValueError(f"legacy state events for {job_id!r} must be a list")
            stored_record = dict(record)
            stored_record.pop("events", None)
            rows.append(
                (
                    stable_id,
                    record.get("canonical_url"),
                    record.get("application_url"),
                    record.get("content_hash"),
                    record.get("status"),
                    json.dumps(stored_record, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    imported_at,
                )
            )
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError(f"legacy state event for {job_id!r} must be an object")
                event_rows.append(
                    (
                        stable_id,
                        _fingerprint(event),
                        str(event.get("at") or imported_at),
                        str(event.get("status") or record.get("status") or "unknown"),
                        event.get("reason"),
                        _json_text(event),
                    )
                )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            already_imported = connection.execute(
                "SELECT 1 FROM migration_log WHERE migration_key = 'legacy-json-import'"
            ).fetchone()
            application_count = connection.execute(
                "SELECT COUNT(*) FROM application_records"
            ).fetchone()[0]
            if already_imported is not None or application_count:
                raise ValueError("legacy import requires an empty SQLite lifecycle")
            connection.executemany(
                """
                INSERT INTO application_records (
                    job_id, canonical_url, application_url, content_hash,
                    status, payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    canonical_url = excluded.canonical_url,
                    application_url = excluded.application_url,
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    imported_at = excluded.imported_at
                """,
                rows,
            )
            connection.executemany(
                "DELETE FROM application_events WHERE job_id = ?",
                [(row[0],) for row in rows],
            )
            connection.executemany(
                """
                INSERT INTO application_events (
                    job_id, event_key, occurred_at, status, reason, event_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                event_rows,
            )
            connection.execute(
                "INSERT INTO migration_log "
                "(migration_key, imported_at, source_path, source_sha256) "
                "VALUES ('legacy-json-import', ?, ?, ?)",
                (imported_at, str(source_path.resolve()), source_sha256),
            )
        return len(rows)

    def legacy_import_matches(self, source_path: Path, source_sha256: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_path, source_sha256 FROM migration_log "
                "WHERE migration_key = 'legacy-json-import'"
            ).fetchone()
        if row is None:
            return False
        if row["source_path"] != str(Path(source_path).resolve()):
            return False
        if not row["source_sha256"] or row["source_sha256"] != source_sha256:
            raise ValueError("legacy state does not match committed import")
        return True

    def legacy_import_completed(self, path: Path) -> bool:
        source_path = Path(path)
        source_sha256 = hashlib.sha256(_read_regular_file(source_path)).hexdigest()
        return self.legacy_import_matches(source_path, source_sha256)

    def record_application_decision(
        self,
        job_id: str,
        record: dict[str, Any],
        *,
        reason: str,
        notify: bool = False,
        transport: str = "photon",
        verification: dict[str, Any] | None = None,
    ) -> None:
        stable_id = str(job_id).strip()
        if not stable_id:
            raise ValueError("job_id must not be empty")
        if not isinstance(record, dict):
            raise ValueError("application record must be an object")
        record_job_id = record.get("job_id")
        if record_job_id is not None and record_job_id != stable_id:
            raise ValueError("record job_id does not match the requested job ID")
        status = record.get("status")
        if status not in VALID_APPLICATION_STATUSES:
            raise ValueError(f"invalid application status: {status!r}")
        if status in {"approved", "blocked", "submitted"}:
            raise ValueError("status is controlled by the approval lifecycle")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("decision reason must not be empty")
        if notify and status != "pending_approval":
            raise ValueError("notifications may only be queued for pending_approval records")
        verification_row: tuple[str, str | None, dict[str, Any]] | None = None
        if verification is not None:
            if not isinstance(verification, dict):
                raise ValueError("verification must be an object")
            verification_outcome = verification.get("outcome")
            verification_evidence = verification.get("evidence")
            verification_url = verification.get("source_url")
            if not isinstance(verification_outcome, str) or not verification_outcome.strip():
                raise ValueError("verification outcome must not be empty")
            if not isinstance(verification_evidence, dict):
                raise ValueError("verification evidence must be an object")
            if verification_url is not None and not isinstance(verification_url, str):
                raise ValueError("verification source URL must be a string")
            verification_row = (
                verification_outcome.strip(),
                verification_url,
                verification_evidence,
            )

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status, payload_json FROM application_records WHERE job_id = ?",
                (stable_id,),
            ).fetchone()
            if existing is not None and existing["status"] == "submitted" and status != "submitted":
                raise ValueError("submitted application cannot regress")
            merged = json.loads(existing["payload_json"]) if existing is not None else {}
            merged.update(record)
            merged.pop("events", None)
            raw_packet_dir = merged.get("packet_dir")
            canonical_packet: Path | None = None
            if raw_packet_dir is not None:
                if not isinstance(raw_packet_dir, str) or not raw_packet_dir.strip():
                    raise ValueError("packet_dir must be a non-empty string")
                canonical_packet = _confined_packet_dir(
                    Path(raw_packet_dir), self.applications_root
                )
                merged["packet_dir"] = str(canonical_packet)
            if notify:
                merged["job_id"] = stable_id
                if canonical_packet is None:
                    raise ValueError("notification requires packet_dir")
                packet_hash, snapshot = packet_snapshot(
                    canonical_packet,
                    applications_root=self.applications_root,
                )
                packet_job = json.loads(snapshot["job.json"])
                if not isinstance(packet_job, dict):
                    raise ValueError("job.json must contain an object")
                if packet_job.get("application_url") != merged.get("application_url"):
                    raise ValueError(
                        "job.json application URL does not match application record"
                    )
                merged["packet_fingerprint"] = packet_hash
                if existing is not None:
                    previous = json.loads(existing["payload_json"])
                    previous_fingerprint = previous.get("packet_fingerprint")
                    packet_changed = (
                        isinstance(previous_fingerprint, str)
                        and previous_fingerprint != merged["packet_fingerprint"]
                    )
                    application_url_changed = (
                        previous.get("application_url") != merged.get("application_url")
                    )
                    if packet_changed or application_url_changed:
                        raise ValueError(
                            "approval-ready packets are immutable; create a new job revision ID"
                        )
            payload_text = _json_text(merged)
            connection.execute(
                """
                INSERT INTO application_records (
                    job_id, canonical_url, application_url, content_hash,
                    status, payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    canonical_url = excluded.canonical_url,
                    application_url = excluded.application_url,
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    imported_at = excluded.imported_at
                """,
                (
                    stable_id,
                    merged.get("canonical_url"),
                    merged.get("application_url"),
                    merged.get("content_hash"),
                    status,
                    payload_text,
                    now,
                ),
            )
            if (
                existing is not None
                and existing["status"] == "pending_approval"
                and status != "pending_approval"
            ):
                connection.execute(
                    "UPDATE delivery_outbox SET status = 'invalidated', "
                    "claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL, "
                    "updated_at = ? WHERE job_id = ? AND status IN ('pending', 'claimed')",
                    (now, stable_id),
                )
            decision_key = _fingerprint(
                {"job_id": stable_id, "status": status, "reason": reason, "record": merged}
            )
            event = {"at": now, "status": status, "reason": reason}
            connection.execute(
                """
                INSERT OR IGNORE INTO application_events (
                    job_id, event_key, occurred_at, status, reason, event_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stable_id, decision_key, now, status, reason, _json_text(event)),
            )
            if verification_row is not None:
                connection.execute(
                    "INSERT INTO verification_attempts "
                    "(job_id, attempted_at, outcome, source_url, evidence_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        stable_id,
                        now,
                        verification_row[0],
                        verification_row[1],
                        _json_text(verification_row[2]),
                    ),
                )
            if notify:
                packet_dir = merged.get("packet_dir")
                if not isinstance(packet_dir, str) or not packet_dir:
                    raise ValueError("pending notification requires packet_dir")
                application_url = merged.get("application_url")
                if not isinstance(application_url, str) or not application_url:
                    raise ValueError("pending notification requires application_url")
                packet_hash = str(merged["packet_fingerprint"])
                outbox_payload = {
                    "job_id": stable_id,
                    "packet_dir": packet_dir,
                    "packet_fingerprint": packet_hash,
                    "application_url": application_url,
                }
                connection.execute(
                    """
                    INSERT OR IGNORE INTO delivery_outbox (
                        job_id, fingerprint, transport, payload_json,
                        status, attempt_count, created_at, updated_at,
                        packet_fingerprint, application_url
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                    """,
                    (
                        stable_id,
                        _fingerprint(merged),
                        transport,
                        _json_text(outbox_payload),
                        now,
                        now,
                        packet_hash,
                        application_url,
                    ),
                )

    def record_verification(
        self,
        job_id: str,
        *,
        outcome: str,
        source_url: str | None,
        evidence: dict[str, Any],
    ) -> int:
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("verification outcome must not be empty")
        if not isinstance(evidence, dict):
            raise ValueError("verification evidence must be an object")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM application_records WHERE job_id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"unknown job ID: {job_id}")
            cursor = connection.execute(
                "INSERT INTO verification_attempts "
                "(job_id, attempted_at, outcome, source_url, evidence_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, now, outcome.strip(), source_url, _json_text(evidence)),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("verification insert did not return an ID")
            return cursor.lastrowid

    def approve_application(self, job_id: str) -> int:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM application_records WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown job ID: {job_id}")
            record = json.loads(row["payload_json"])
            if record.get("status") != "pending_approval":
                raise ValueError("application is not pending approval")
            application_url = record.get("application_url")
            if not isinstance(application_url, str) or not application_url:
                raise ValueError("application approval requires an application URL")
            packet_hash = packet_fingerprint(
                Path(str(record.get("packet_dir") or "")),
                applications_root=self.applications_root,
            )
            if packet_hash != record.get("packet_fingerprint"):
                raise ValueError("packet changed before approval")
            dispatched = connection.execute(
                "SELECT 1 FROM delivery_outbox "
                "WHERE job_id = ? AND packet_fingerprint = ? AND application_url = ? "
                "AND status IN ('dispatched', 'delivered') LIMIT 1",
                (job_id, packet_hash, application_url),
            ).fetchone()
            if dispatched is None:
                raise ValueError("approval notification has not been dispatched")
            connection.execute(
                "UPDATE approvals SET status = 'invalidated', invalidated_at = ? "
                "WHERE job_id = ? AND status = 'active'",
                (now, job_id),
            )
            cursor = connection.execute(
                "INSERT INTO approvals (job_id, packet_fingerprint, application_url, approved_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, packet_hash, application_url, now),
            )
            record["status"] = "approved"
            connection.execute(
                "UPDATE application_records SET status = 'approved', payload_json = ?, imported_at = ? "
                "WHERE job_id = ?",
                (_json_text(record), now, job_id),
            )
            event = {"at": now, "status": "approved", "reason": "Exact packet approved"}
            connection.execute(
                "INSERT INTO application_events "
                "(job_id, event_key, occurred_at, status, reason, event_json) "
                "VALUES (?, ?, ?, 'approved', ?, ?)",
                (job_id, _fingerprint(event), now, event["reason"], _json_text(event)),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("approval insert did not return an ID")
            return cursor.lastrowid

    def record_submission(
        self,
        job_id: str,
        *,
        outcome: str,
        confirmation: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        if outcome not in {"submitted", "blocked"}:
            raise ValueError(f"invalid submission outcome: {outcome!r}")
        if outcome == "submitted" and not confirmation:
            raise ValueError("submitted outcome requires confirmation evidence")
        if outcome == "blocked" and (not isinstance(error, str) or not error.strip()):
            raise ValueError("blocked outcome requires an error")
        blocked_reason = error.strip() if isinstance(error, str) else None
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, payload_json FROM application_records WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown job ID: {job_id}")
            record = json.loads(row["payload_json"])
            if row["status"] not in {"approved", "blocked"}:
                raise ValueError("application has no active exact-job approval")
            approval = connection.execute(
                "SELECT approval_id, packet_fingerprint, application_url FROM approvals "
                "WHERE job_id = ? AND status = 'active' ORDER BY approval_id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if approval is None:
                raise ValueError("application has no active exact-job approval")
            packet_hash = packet_fingerprint(
                Path(str(record.get("packet_dir") or "")),
                applications_root=self.applications_root,
            )
            if packet_hash != approval["packet_fingerprint"]:
                raise ValueError("packet changed after approval")
            if record.get("application_url") != approval["application_url"]:
                raise ValueError("application URL changed after approval")
            cursor = connection.execute(
                "INSERT INTO submission_attempts "
                "(job_id, approval_id, attempted_at, outcome, confirmation_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    approval["approval_id"],
                    now,
                    outcome,
                    _json_text(confirmation) if confirmation is not None else None,
                    blocked_reason,
                ),
            )
            if outcome == "submitted":
                consumed = connection.execute(
                    "UPDATE approvals SET status = 'consumed', consumed_at = ? "
                    "WHERE approval_id = ? AND status = 'active'",
                    (now, approval["approval_id"]),
                )
                if consumed.rowcount != 1:
                    raise ValueError("application has no active exact-job approval")
            record["status"] = outcome
            if confirmation is not None:
                record["submission_confirmation"] = confirmation
            if blocked_reason:
                record["submission_error"] = blocked_reason
            connection.execute(
                "UPDATE application_records SET status = ?, payload_json = ?, imported_at = ? "
                "WHERE job_id = ?",
                (outcome, _json_text(record), now, job_id),
            )
            reason = "Submission verified" if outcome == "submitted" else blocked_reason
            event = {"at": now, "status": outcome, "reason": reason}
            connection.execute(
                "INSERT INTO application_events "
                "(job_id, event_key, occurred_at, status, reason, event_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, _fingerprint(event), now, outcome, reason, _json_text(event)),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("submission insert did not return an ID")
            return cursor.lastrowid

    def get_application_record(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM application_records WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            events = connection.execute(
                "SELECT event_json FROM application_events "
                "WHERE job_id = ? ORDER BY occurred_at, event_id",
                (job_id,),
            ).fetchall()
        record = json.loads(row["payload_json"])
        record["events"] = [json.loads(event["event_json"]) for event in events]
        return record

    def list_application_records(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT job_id FROM application_records ORDER BY imported_at, job_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT job_id FROM application_records WHERE status = ? "
                    "ORDER BY imported_at, job_id",
                    (status,),
                ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = self.get_application_record(str(row["job_id"]))
            if record is not None:
                records.append({"job_id": str(row["job_id"]), **record})
        return records

    def list_outbox(self, status: str = "pending") -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT outbox_id, job_id, fingerprint, transport, payload_json, "
                "status, attempt_count, created_at, updated_at, delivered_at, last_error, "
                "packet_fingerprint, application_url, claim_token, claimed_at, "
                "lease_expires_at "
                "FROM delivery_outbox WHERE status = ? ORDER BY created_at, outbox_id",
                (status,),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "payload_json"},
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def claim_outbox(self, *, limit: int = 25, lease_seconds: int = 300) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("outbox claim limit must be an integer from 1 to 100")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("outbox lease must be an integer from 1 to 3600 seconds")
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat().replace("+00:00", "Z")
        lease_expires_at = (now_value + timedelta(seconds=lease_seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE delivery_outbox SET status = 'pending', claim_token = NULL, "
                "claimed_at = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE status = 'claimed' AND lease_expires_at <= ?",
                (now, now),
            )
            rows = connection.execute(
                "SELECT outbox_id FROM delivery_outbox WHERE status = 'pending' "
                "ORDER BY created_at, outbox_id LIMIT ?",
                (limit,),
            ).fetchall()
            if not rows:
                return {"claim_token": None, "items": []}
            connection.executemany(
                "UPDATE delivery_outbox SET status = 'claimed', claim_token = ?, "
                "claimed_at = ?, lease_expires_at = ?, updated_at = ?, "
                "attempt_count = attempt_count + 1 "
                "WHERE outbox_id = ? AND status = 'pending'",
                [
                    (claim_token, now, lease_expires_at, now, int(row["outbox_id"]))
                    for row in rows
                ],
            )
            claimed = connection.execute(
                "SELECT outbox_id, job_id, fingerprint, transport, payload_json, "
                "status, attempt_count, created_at, updated_at, delivered_at, last_error, "
                "packet_fingerprint, application_url, claim_token, claimed_at, "
                "lease_expires_at FROM delivery_outbox WHERE claim_token = ? "
                "ORDER BY created_at, outbox_id",
                (claim_token,),
            ).fetchall()
        return {
            "claim_token": claim_token,
            "items": [
                {
                    **{key: row[key] for key in row.keys() if key != "payload_json"},
                    "payload": json.loads(row["payload_json"]),
                }
                for row in claimed
            ],
        }

    def release_outbox_claim(self, claim_token: str, error: str) -> bool:
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim token must not be empty")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("claim release error must not be empty")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE delivery_outbox SET status = 'pending', claim_token = NULL, "
                "claimed_at = NULL, lease_expires_at = NULL, updated_at = ?, last_error = ? "
                "WHERE status = 'claimed' AND claim_token = ?",
                (now, error.strip(), claim_token),
            )
        return cursor.rowcount > 0

    def mark_outbox_claim_dispatched(self, claim_token: str) -> int:
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim token must not be empty")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE delivery_outbox SET status = 'dispatched', lease_expires_at = NULL, "
                "updated_at = ?, last_error = NULL "
                "WHERE status = 'claimed' AND claim_token = ?",
                (now, claim_token),
            )
        return cursor.rowcount

    def mark_outbox_dispatched(self, outbox_ids: list[int]) -> int:
        if not outbox_ids:
            return 0
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            before = connection.total_changes
            connection.executemany(
                "UPDATE delivery_outbox SET status = 'dispatched', "
                "attempt_count = attempt_count + 1, updated_at = ? "
                "WHERE status = 'pending' AND outbox_id = ?",
                [(now, outbox_id) for outbox_id in outbox_ids],
            )
            changed = connection.total_changes - before
        return changed

    def mark_outbox_delivered(self, outbox_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE delivery_outbox SET status = 'delivered', updated_at = ?, "
                "delivered_at = ?, last_error = NULL "
                "WHERE outbox_id = ? AND status = 'dispatched'",
                (now, now, outbox_id),
            )
        return cursor.rowcount == 1

    def mark_outbox_failed(self, outbox_id: int, error: str) -> bool:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("delivery error must not be empty")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE delivery_outbox SET status = 'failed', updated_at = ?, "
                "last_error = ? WHERE outbox_id = ? "
                "AND status IN ('pending', 'dispatched')",
                (now, error.strip(), outbox_id),
            )
        return cursor.rowcount == 1

    def retry_outbox(self, outbox_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE delivery_outbox SET status = 'pending', updated_at = ?, "
                "delivered_at = NULL, last_error = NULL "
                "WHERE outbox_id = ? AND status = 'failed'",
                (now, outbox_id),
            )
        return cursor.rowcount == 1

    def get_queue_cursor(self, queue_name: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_key FROM queue_cursors WHERE queue_name = ?",
                (queue_name,),
            ).fetchone()
        return str(row["last_key"]) if row is not None else ""

    def set_queue_cursor(self, queue_name: str, last_key: str) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO queue_cursors (queue_name, last_key, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(queue_name) DO UPDATE SET
                    last_key = excluded.last_key,
                    updated_at = excluded.updated_at
                """,
                (queue_name, last_key, now),
            )

    def export_legacy_json(self, path: Path) -> int:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, payload_json FROM application_records ORDER BY job_id"
            ).fetchall()
        records = {}
        for row in rows:
            record = self.get_application_record(str(row["job_id"]))
            if record is not None:
                records[str(row["job_id"])] = record
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "jobs": records,
        }
        fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return len(rows)

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "application_records": connection.execute(
                    "SELECT COUNT(*) FROM application_records"
                ).fetchone()[0],
                "candidates": connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
                "collector_runs": connection.execute(
                    "SELECT COUNT(*) FROM collector_runs"
                ).fetchone()[0],
                "application_events": connection.execute(
                    "SELECT COUNT(*) FROM application_events"
                ).fetchone()[0],
                "delivery_outbox": connection.execute(
                    "SELECT COUNT(*) FROM delivery_outbox"
                ).fetchone()[0],
                "queue_cursors": connection.execute(
                    "SELECT COUNT(*) FROM queue_cursors"
                ).fetchone()[0],
                "verification_attempts": connection.execute(
                    "SELECT COUNT(*) FROM verification_attempts"
                ).fetchone()[0],
                "approvals": connection.execute(
                    "SELECT COUNT(*) FROM approvals"
                ).fetchone()[0],
                "submission_attempts": connection.execute(
                    "SELECT COUNT(*) FROM submission_attempts"
                ).fetchone()[0],
            }

    def record_pipeline_result(self, result: Any) -> str:
        recorded_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_id = uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO collector_runs (
                    run_id, started_at, accepted_count, configured_sources,
                    successful_sources, source_failures_json, rejected_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    recorded_at,
                    len(result.jobs),
                    result.configured_sources,
                    result.successful_sources,
                    json.dumps(
                        [failure.__dict__ for failure in result.source_failures],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    json.dumps(result.rejected_counts, separators=(",", ":"), sort_keys=True),
                ),
            )
            for candidate in result.jobs:
                payload = candidate.to_dict()
                connection.execute(
                    """
                    INSERT INTO candidates (
                        source, tenant, source_id, canonical_url, application_url,
                        content_hash, payload_json, first_seen_at, last_seen_at, last_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, tenant, source_id) DO UPDATE SET
                        canonical_url = excluded.canonical_url,
                        application_url = excluded.application_url,
                        content_hash = excluded.content_hash,
                        payload_json = excluded.payload_json,
                        last_seen_at = excluded.last_seen_at,
                        last_run_id = excluded.last_run_id
                    """,
                    (
                        candidate.source,
                        candidate.tenant,
                        candidate.source_id,
                        candidate.canonical_url,
                        candidate.apply_url,
                        payload["content_hash"],
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                        recorded_at,
                        recorded_at,
                        run_id,
                    ),
                )
        return run_id

    def known_record_entries(self) -> list[tuple[str, str | None]]:
        entries: list[tuple[str, str | None]] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT canonical_url, application_url, content_hash FROM candidates
                UNION ALL
                SELECT canonical_url, application_url, content_hash FROM application_records
                """
            )
            for row in rows:
                content_hash = str(row["content_hash"]) if row["content_hash"] else None
                for url in (row["canonical_url"], row["application_url"]):
                    if url:
                        entries.append((str(url), content_hash))
        return entries

    def application_dedupe_entries(self) -> list[tuple[str, str | None]]:
        """Return only application decisions that may suppress collection.

        Raw collector candidates remain eligible until the orchestrator records
        a verified decision in the application state. This makes interrupted or
        oversized batches retryable on the next scheduled run.
        """
        entries: list[tuple[str, str | None]] = []
        decision_statuses = (
            "rejected",
            "pending_approval",
            "approved",
            "blocked",
            "submitted",
            "skipped",
            "closed",
        )
        placeholders = ", ".join("?" for _ in decision_statuses)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT canonical_url, application_url, content_hash "
                f"FROM application_records WHERE status IN ({placeholders})",
                decision_statuses,
            )
            for row in rows:
                content_hash = str(row["content_hash"]) if row["content_hash"] else None
                for url in (row["canonical_url"], row["application_url"]):
                    if url:
                        entries.append((str(url), content_hash))
        return entries

    def known_records(self) -> dict[str, str | None]:
        known: dict[str, str | None] = {}
        for url, content_hash in self.known_record_entries():
            if url not in known or content_hash is None:
                known[url] = content_hash
        return known
