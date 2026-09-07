from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from typing import Callable, Mapping

from job_scout.state_store import packet_fingerprint


MAX_BACKUP_FILE_BYTES = 256 * 1024 * 1024
MAX_BACKUP_TOTAL_BYTES = 500 * 1024 * 1024
MAX_BACKUP_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_BACKUP_FILES = 10_000

@dataclass(frozen=True)
class TectonicRelease:
    url: str
    sha256: str


_TECTONIC_VERSION = "0.17.0"
_TECTONIC_BASE = (
    "https://github.com/tectonic-typesetting/tectonic/releases/download/"
    "tectonic%400.17.0/"
)
_TECTONIC_RELEASES = {
    ("Linux", "x86_64"): (
        "tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz",
        "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7",
    ),
    ("Linux", "aarch64"): (
        "tectonic-0.17.0-aarch64-unknown-linux-musl.tar.gz",
        "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8",
    ),
    ("Darwin", "x86_64"): (
        "tectonic-0.17.0-x86_64-apple-darwin.tar.gz",
        "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80",
    ),
    ("Darwin", "arm64"): (
        "tectonic-0.17.0-aarch64-apple-darwin.tar.gz",
        "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667",
    ),
}


def tectonic_release(system: str, machine: str) -> TectonicRelease:
    normalized_machine = "aarch64" if machine == "arm64" and system == "Linux" else machine
    try:
        filename, digest = _TECTONIC_RELEASES[(system, normalized_machine)]
    except KeyError as error:
        raise ValueError(f"unsupported platform for Tectonic: {system}/{machine}") from error
    return TectonicRelease(_TECTONIC_BASE + filename, digest)


def render_template(text: str, values: Mapping[str, str]) -> str:
    rendered = text
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    unresolved = re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", rendered)
    if unresolved:
        raise ValueError(
            f"unresolved template placeholder: {unresolved.group(0)}"
        )
    return rendered


def node_version_supported(output: str) -> bool:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", output.strip())
    if not match:
        return False
    return tuple(int(part) for part in match.groups()) >= (18, 17, 0)


def install_tectonic_archive(
    archive: bytes,
    *,
    expected_sha256: str,
    destination: Path,
) -> None:
    if hashlib.sha256(archive).hexdigest() != expected_sha256:
        raise ValueError("Tectonic archive checksum mismatch")
    if len(archive) > 64 * 1024 * 1024:
        raise ValueError("Tectonic archive exceeds size limit")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            candidates = [
                member
                for member in bundle.getmembers()
                if member.isfile() and Path(member.name).name == "tectonic"
            ]
            if len(candidates) != 1:
                raise ValueError("Tectonic archive must contain one executable")
            member = candidates[0]
            if not _safe_archive_name(member.name) or member.size > 64 * 1024 * 1024:
                raise ValueError("unsafe Tectonic archive member")
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise ValueError("Tectonic executable is unreadable")
            binary = extracted.read(64 * 1024 * 1024 + 1)
    except (tarfile.TarError, OSError) as error:
        raise ValueError("invalid Tectonic archive") from error
    if len(binary) != member.size or len(binary) > 64 * 1024 * 1024:
        raise ValueError("invalid Tectonic executable size")
    destination = Path(destination)
    _reject_symlink_ancestors(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_ancestors(destination)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.install-",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(binary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o755)
        os.replace(temporary_name, destination)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


@dataclass(frozen=True)
class RuntimePaths:
    automation: Path
    workspace: Path
    resume_dir: Path
    hermes_home: Path
    resume: Path
    tectonic: Path

    @classmethod
    def from_environment(
        cls,
        repository_root: Path,
        *,
        home: Path,
        environment: Mapping[str, str],
    ) -> "RuntimePaths":
        def resolve(name: str, default: Path) -> Path:
            raw = environment.get(name)
            if not raw:
                return default.resolve()
            if raw == "~" or raw.startswith("~/"):
                raw = str(home) + raw[1:]
            return Path(raw).resolve()

        automation = resolve("JOB_SEARCH_AUTOMATION", repository_root)
        workspace = resolve("JOB_SEARCH_WORKDIR", home / "job-search")
        resume_dir = resolve("RESUME_DIR", home / "job-search-resume")
        base_hermes_home = resolve("HERMES_HOME", home / ".hermes")
        hermes_home = resolve("JOB_SEARCH_HERMES_HOME", base_hermes_home)
        default_resume = resume_dir / "master-resume.tex"
        resume = resolve("MASTER_RESUME_FILE", default_resume)
        if "MASTER_RESUME_FILE" not in environment and not resume.exists():
            existing_resumes = sorted(resume_dir.glob("*.tex")) if resume_dir.is_dir() else []
            if len(existing_resumes) == 1:
                resume = existing_resumes[0].resolve()
        tectonic = resolve("TECTONIC", home / ".local/bin/tectonic")
        return cls(automation, workspace, resume_dir, hermes_home, resume, tectonic)

    @property
    def database(self) -> Path:
        return self.workspace / "job-scout.db"

    @property
    def search_profile(self) -> Path:
        return self.workspace / "search-profile.json"

    @property
    def application_profile(self) -> Path:
        return self.workspace / "application-profile.json"

    @property
    def applications(self) -> Path:
        return self.workspace / "applications"

    @property
    def source_registry(self) -> Path:
        return self.automation / "config/sources.json"

    @property
    def skill(self) -> Path:
        return (
            self.hermes_home
            / "skills/productivity/continuous-job-search/SKILL.md"
        )


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    required: bool
    detail: str


def _sqlite_integrity(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"missing: {path}"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        return False, str(error)
    value = str(result[0]) if result else "no result"
    return value == "ok", value


def doctor(
    paths: RuntimePaths,
    *,
    command_lookup: Callable[[str], str | None] = shutil.which,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for command, label, required in (
        ("git", "Git", True),
        ("curl", "curl", True),
        ("python3", "Python 3", True),
        ("hermes", "Hermes CLI", True),
        ("npm", "Photon/npm", False),
    ):
        found = command_lookup(command)
        checks.append(
            DoctorCheck(label, found is not None, required, found or "not installed")
        )
    node_command = command_lookup("node")
    node_output = "not installed"
    node_ok = False
    if node_command is not None:
        try:
            completed = subprocess.run(
                [node_command, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            node_output = (completed.stdout or completed.stderr).strip()
            node_ok = completed.returncode == 0 and node_version_supported(node_output)
        except (OSError, subprocess.SubprocessError) as error:
            node_output = str(error)
    checks.append(
        DoctorCheck(
            "Photon/Node.js",
            node_ok,
            False,
            node_output if node_command else "not installed; requires >=18.17",
        )
    )
    tectonic_command = command_lookup("tectonic")
    tectonic_detail = tectonic_command or str(paths.tectonic)
    checks.append(
        DoctorCheck(
            "Tectonic",
            tectonic_command is not None or paths.tectonic.is_file(),
            True,
            tectonic_detail,
        )
    )

    for path, label in (
        (paths.search_profile, "Search profile"),
        (paths.application_profile, "Application profile"),
        (paths.resume, "Master resume"),
        (paths.skill, "Hermes job-search skill"),
    ):
        checks.append(DoctorCheck(label, path.is_file(), True, str(path)))

    for path, label in (
        (paths.search_profile, "Search profile JSON"),
        (paths.application_profile, "Application profile JSON"),
    ):
        if not path.is_file():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            ok = isinstance(parsed, dict)
            detail = "valid object" if ok else "top level must be an object"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            ok, detail = False, str(error)
        checks.append(DoctorCheck(label, ok, True, detail))

    try:
        search_data = json.loads(paths.search_profile.read_text(encoding="utf-8"))
        candidate = search_data.get("candidate") if isinstance(search_data, dict) else None
        candidate_name = candidate.get("name") if isinstance(candidate, dict) else None
        candidate_ready = (
            isinstance(candidate_name, str)
            and bool(candidate_name.strip())
            and candidate_name.strip() != "Candidate Name"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        candidate_name, candidate_ready = None, False
    checks.append(
        DoctorCheck(
            "Candidate profile configured",
            candidate_ready,
            True,
            str(candidate_name or "candidate.name is not configured"),
        )
    )
    try:
        application_data = json.loads(
            paths.application_profile.read_text(encoding="utf-8")
        )
        application_status = (
            application_data.get("status") if isinstance(application_data, dict) else None
        )
        application_ready = (
            isinstance(application_status, str)
            and bool(application_status.strip())
            and application_status != "configuration_required"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        application_status, application_ready = None, False
    checks.append(
        DoctorCheck(
            "Application profile configured",
            application_ready,
            True,
            str(application_status or "status is not configured"),
        )
    )

    database_ok, database_detail = _sqlite_integrity(paths.database)
    checks.append(
        DoctorCheck("SQLite database", database_ok, True, database_detail)
    )
    checks.append(
        DoctorCheck(
            "Direct ATS registry",
            paths.source_registry.is_file(),
            False,
            str(paths.source_registry),
        )
    )
    return checks


def _file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_bytes(bundle: tarfile.TarFile, name: str, data: bytes, mode: int = 0o600) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    member.mtime = 0
    bundle.addfile(member, io.BytesIO(data))


def _bounded_read(path: Path, remaining: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"backup source is not a regular file: {path}")
        limit = min(MAX_BACKUP_FILE_BYTES, remaining)
        if status.st_size > limit:
            raise ValueError("backup file exceeds size limit")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit - size + 1)):
            size += len(chunk)
            if size > limit:
                raise ValueError("backup file exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_allowed_files(paths: RuntimePaths, database_snapshot: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0

    def add(source: Path, archive_name: str) -> None:
        nonlocal total
        if not _allowed_archive_name(archive_name):
            raise ValueError(f"unsafe archive path generated from backup source: {archive_name}")
        if len(files) >= MAX_BACKUP_FILES:
            raise ValueError("backup exceeds file-count limit")
        data = _bounded_read(source, MAX_BACKUP_TOTAL_BYTES - total)
        total += len(data)
        files[archive_name] = data

    add(database_snapshot, "workspace/job-scout.db")
    optional = (
        (paths.search_profile, "workspace/search-profile.json"),
        (paths.application_profile, "workspace/application-profile.json"),
        (paths.resume, "resume/master-resume.tex"),
        (paths.source_registry, "automation-private/sources.json"),
    )
    for source, archive_name in optional:
        if source.is_file():
            if source.is_symlink():
                raise ValueError(f"backup source cannot be a symlink: {source}")
            add(source, archive_name)
    if paths.applications.is_dir():
        for source in sorted(paths.applications.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"backup source cannot be a symlink: {source}")
            if source.is_file():
                relative = source.relative_to(paths.applications).as_posix()
                add(source, f"workspace/applications/{relative}")
    return files


def _backup_runtime_locked(
    paths: RuntimePaths,
    archive: Path,
    *,
    source_commit: str = "unknown",
) -> dict[str, object]:
    if not paths.database.is_file():
        raise FileNotFoundError(f"database does not exist: {paths.database}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(archive)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(archive.parent, directory_flags)
    if not stat.S_ISDIR(os.fstat(parent_descriptor).st_mode):
        os.close(parent_descriptor)
        raise ValueError(f"backup output parent is not a directory: {archive.parent}")
    try:
        try:
            output_status = os.stat(
                archive.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            output_status = None
        if output_status is not None and stat.S_ISLNK(output_status.st_mode):
            raise ValueError(f"backup output cannot be a symlink: {archive}")
        return _write_backup_archive_locked(
            paths,
            archive,
            parent_descriptor=parent_descriptor,
            source_commit=source_commit,
        )
    finally:
        os.close(parent_descriptor)


def _write_backup_archive_locked(
    paths: RuntimePaths,
    archive: Path,
    *,
    parent_descriptor: int,
    source_commit: str,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(dir=archive.parent) as temporary_directory:
        snapshot = Path(temporary_directory) / "job-scout.db"
        with sqlite3.connect(paths.database) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
        ok, detail = _sqlite_integrity(snapshot)
        if not ok:
            raise ValueError(f"database snapshot failed integrity check: {detail}")
        files = _read_allowed_files(paths, snapshot)
        _validate_packet_payloads(
            snapshot,
            payloads=files,
            stored_applications_root=paths.applications,
            staging_root=Path(temporary_directory) / "packet-validation",
        )
        if len(files) > MAX_BACKUP_FILES:
            raise ValueError("backup exceeds file-count limit")
        if any(len(data) > MAX_BACKUP_FILE_BYTES for data in files.values()):
            raise ValueError("backup file exceeds size limit")
        if sum(len(data) for data in files.values()) > MAX_BACKUP_TOTAL_BYTES:
            raise ValueError("backup exceeds total size limit")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": source_commit,
            "applications_root": str(paths.applications.resolve()),
            "files": {name: _file_digest(data) for name, data in sorted(files.items())},
        }
        temporary_archive = Path(temporary_directory) / "backup.tar.gz"
        with tarfile.open(temporary_archive, "w:gz", format=tarfile.USTAR_FORMAT) as bundle:
            _add_bytes(
                bundle,
                "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            for name, data in sorted(files.items()):
                _add_bytes(bundle, name, data)
        os.chmod(temporary_archive, 0o600)
        if temporary_archive.stat().st_size > MAX_BACKUP_ARCHIVE_BYTES:
            raise ValueError("compressed backup exceeds size limit")
        temporary_directory_descriptor = os.open(
            temporary_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.replace(
                temporary_archive.name,
                archive.name,
                src_dir_fd=temporary_directory_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        finally:
            os.close(temporary_directory_descriptor)
    return manifest


def backup_runtime(
    paths: RuntimePaths,
    archive: Path,
    *,
    source_commit: str = "unknown",
) -> dict[str, object]:
    archive_target = Path(os.path.abspath(archive))
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(archive_target)
    resolved_archive_target = archive_target.parent.resolve() / archive_target.name
    protected_roots = (
        paths.workspace.resolve(),
        paths.resume_dir.resolve(),
        paths.automation.resolve(),
        paths.hermes_home.resolve(),
    )
    if any(
        resolved_archive_target == root or resolved_archive_target.is_relative_to(root)
        for root in protected_roots
    ):
        raise ValueError("backup archive must be outside runtime inputs")
    for source in (
        paths.workspace,
        paths.database,
        paths.applications,
        paths.resume,
        paths.source_registry,
    ):
        _reject_symlink_ancestors(source)
    lock_path = paths.database.with_name(f"{paths.database.name}.run.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"run lock is not a regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _backup_runtime_locked(
            paths,
            archive_target,
            source_commit=source_commit,
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or "\0" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(
        part not in ("", ".", "..") for part in name.split("/")
    )


def _allowed_archive_name(name: str) -> bool:
    if not _safe_archive_name(name):
        return False
    if name in {
        "workspace/job-scout.db",
        "workspace/search-profile.json",
        "workspace/application-profile.json",
        "resume/master-resume.tex",
        "automation-private/sources.json",
    }:
        return True
    return name.startswith("workspace/applications/")


def _destination_for(paths: RuntimePaths, name: str) -> Path:
    if not _allowed_archive_name(name):
        raise ValueError(f"unsupported archive member: {name}")
    if name == "resume/master-resume.tex":
        return paths.resume
    mappings = {
        "workspace/": paths.workspace,
        "automation-private/": paths.automation / "config",
    }
    for prefix, root in mappings.items():
        if name.startswith(prefix):
            return root / PurePosixPath(name.removeprefix(prefix))
    raise ValueError(f"unsupported archive member: {name}")


def _reject_symlink_ancestors(destination: Path) -> None:
    current = destination
    while True:
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError(f"restore destination contains a symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _ensure_empty(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"restore destination is not empty: {path}")


def _relocated_packet_dir(
    raw_packet: object,
    *,
    old_root: Path,
    new_root: Path,
    source: str,
) -> str:
    if not isinstance(raw_packet, str) or not raw_packet.strip():
        raise ValueError(f"invalid {source} packet_dir during restore")
    packet = Path(raw_packet)
    if not packet.is_absolute():
        packet = old_root / packet
    try:
        relative = packet.resolve().relative_to(old_root)
    except ValueError as error:
        raise ValueError(
            f"{source} packet_dir is outside archived applications root"
        ) from error
    return str(new_root / relative)


def _validate_packet_fingerprints(
    database: Path,
    *,
    stored_applications_root: Path,
    actual_applications_root: Path,
) -> None:
    stored_root = Path(stored_applications_root).resolve()
    actual_root = Path(actual_applications_root).resolve()
    actual_by_job: dict[str, str] = {}
    records: dict[str, dict[str, object]] = {}
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "application_records" not in tables:
            return
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(application_records)")
        }
        if not {"job_id", "payload_json"}.issubset(columns):
            return
        for row in connection.execute(
            "SELECT job_id, payload_json FROM application_records"
        ):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("invalid application_records payload during backup") from error
            if not isinstance(payload, dict):
                raise ValueError("invalid application_records payload during backup")
            job_id = str(row["job_id"])
            records[job_id] = payload
            expected = payload.get("packet_fingerprint")
            if expected is None:
                continue
            packet_dir = _relocated_packet_dir(
                payload.get("packet_dir"),
                old_root=stored_root,
                new_root=actual_root,
                source="application_records",
            )
            actual = packet_fingerprint(Path(packet_dir), applications_root=actual_root)
            if expected != actual:
                raise ValueError(f"packet fingerprint mismatch: {job_id}")
            actual_by_job[job_id] = actual

        if "approvals" in tables:
            for job_id, expected in connection.execute(
                "SELECT job_id, packet_fingerprint FROM approvals"
            ):
                if expected is not None and actual_by_job.get(str(job_id)) != expected:
                    raise ValueError(f"packet fingerprint mismatch: {job_id}")

        if "delivery_outbox" in tables:
            outbox_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(delivery_outbox)")
            }
            if {"job_id", "packet_fingerprint", "payload_json"}.issubset(outbox_columns):
                for job_id, expected, payload_text in connection.execute(
                    "SELECT job_id, packet_fingerprint, payload_json FROM delivery_outbox"
                ):
                    if expected is not None and actual_by_job.get(str(job_id)) != expected:
                        raise ValueError(f"packet fingerprint mismatch: {job_id}")
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ValueError("invalid delivery_outbox payload during backup") from error
                    payload_expected = payload.get("packet_fingerprint") if isinstance(payload, dict) else None
                    if payload_expected is not None and actual_by_job.get(str(job_id)) != payload_expected:
                        raise ValueError(f"packet fingerprint mismatch: {job_id}")


def _validate_packet_payloads(
    database: Path,
    *,
    payloads: Mapping[str, bytes],
    stored_applications_root: Path,
    staging_root: Path,
) -> None:
    staged_applications = staging_root / "applications"
    prefix = "workspace/applications/"
    for name, data in payloads.items():
        if name.startswith(prefix):
            staged_file = staged_applications / PurePosixPath(name.removeprefix(prefix))
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            staged_file.write_bytes(data)
    _validate_packet_fingerprints(
        database,
        stored_applications_root=stored_applications_root,
        actual_applications_root=staged_applications,
    )


def _relocate_packet_paths(
    database: Path,
    *,
    old_applications_root: Path,
    new_applications_root: Path,
) -> None:
    old_root = old_applications_root.resolve()
    new_root = new_applications_root.resolve()
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        table_statements = {
            "application_records": (
                "PRAGMA table_info(application_records)",
                "SELECT rowid, payload_json FROM application_records",
                "UPDATE application_records SET payload_json = ? WHERE rowid = ?",
            ),
            "delivery_outbox": (
                "PRAGMA table_info(delivery_outbox)",
                "SELECT rowid, payload_json FROM delivery_outbox",
                "UPDATE delivery_outbox SET payload_json = ? WHERE rowid = ?",
            ),
        }
        for table, (column_query, select_query, update_query) in table_statements.items():
            if table not in tables:
                continue
            columns = {
                row[1]
                for row in connection.execute(column_query)
            }
            if "payload_json" not in columns:
                continue
            updates: list[tuple[str, int]] = []
            for rowid, payload_text in connection.execute(select_query):
                try:
                    payload = json.loads(payload_text)
                except (TypeError, json.JSONDecodeError) as error:
                    raise ValueError(f"invalid {table} payload during restore") from error
                if not isinstance(payload, dict) or "packet_dir" not in payload:
                    continue
                payload["packet_dir"] = _relocated_packet_dir(
                    payload.get("packet_dir"),
                    old_root=old_root,
                    new_root=new_root,
                    source=table,
                )
                updates.append(
                    (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True), rowid)
                )
            connection.executemany(update_query, updates)


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_bounded_ustar(archive_file) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    total_size = 0
    headers = 0
    zero_blocks = 0
    extension_types = {b"x", b"g", b"L", b"K", b"X"}
    try:
        with gzip.GzipFile(fileobj=archive_file, mode="rb") as stream:
            while True:
                header = _read_exact(stream, tarfile.BLOCKSIZE)
                if not header:
                    break
                if len(header) != tarfile.BLOCKSIZE:
                    raise ValueError("truncated backup archive header")
                if header == b"\0" * tarfile.BLOCKSIZE:
                    zero_blocks += 1
                    if zero_blocks == 2:
                        break
                    continue
                zero_blocks = 0
                headers += 1
                if headers > MAX_BACKUP_FILES + 1:
                    raise ValueError("backup exceeds file-count limit")
                if header[156:157] in extension_types:
                    raise ValueError("tar extension headers are not supported")
                member = tarfile.TarInfo.frombuf(
                    header, encoding="utf-8", errors="surrogateescape"
                )
                if not member.isreg():
                    raise ValueError(f"unsupported archive member type: {member.name}")
                if member.name in payloads:
                    raise ValueError("duplicate archive member name")
                if not _safe_archive_name(member.name):
                    raise ValueError(f"unsafe archive path: {member.name}")
                if member.size > MAX_BACKUP_FILE_BYTES:
                    raise ValueError("backup member exceeds size limit")
                total_size += member.size
                if total_size > MAX_BACKUP_TOTAL_BYTES:
                    raise ValueError("backup exceeds total size limit")
                data = _read_exact(stream, member.size)
                if len(data) != member.size:
                    raise ValueError(f"archive member size mismatch: {member.name}")
                padding = (-member.size) % tarfile.BLOCKSIZE
                if len(_read_exact(stream, padding)) != padding:
                    raise ValueError("truncated backup archive padding")
                payloads[member.name] = data
    except (gzip.BadGzipFile, tarfile.TarError, OSError, UnicodeError) as error:
        raise ValueError("invalid backup archive") from error
    if zero_blocks != 2:
        raise ValueError("backup archive is missing USTAR end markers")
    return payloads


def restore_runtime(archive: Path, paths: RuntimePaths) -> dict[str, object]:
    _ensure_empty(paths.workspace)
    _ensure_empty(paths.resume_dir)
    archive_path = Path(archive)
    archive_descriptor = os.open(
        archive_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        archive_status = os.fstat(archive_descriptor)
        if not stat.S_ISREG(archive_status.st_mode):
            raise ValueError("backup archive must be a regular file")
        if archive_status.st_size > MAX_BACKUP_ARCHIVE_BYTES:
            raise ValueError("compressed backup exceeds size limit")
        with os.fdopen(archive_descriptor, "rb", closefd=False) as archive_file:
            all_payloads = _read_bounded_ustar(archive_file)
    finally:
        os.close(archive_descriptor)

    manifest_data = all_payloads.pop("manifest.json", None)
    if manifest_data is None:
        raise ValueError("backup has no manifest")
    try:
        manifest = json.loads(manifest_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid backup manifest") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported backup schema")
    archived_applications_root = manifest.get("applications_root")
    if not isinstance(archived_applications_root, str) or not Path(
        archived_applications_root
    ).is_absolute():
        raise ValueError("backup manifest has no absolute applications root")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise ValueError("backup manifest has no file inventory")
    if set(all_payloads) != set(expected):
        raise ValueError("backup file inventory mismatch")
    payloads: dict[str, bytes] = {}
    for name, digest in expected.items():
        if not isinstance(name, str) or not _safe_archive_name(name):
            raise ValueError("unsafe archive path")
        if not _allowed_archive_name(name):
            raise ValueError(f"unsupported archive member: {name}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid backup digest")
        data = all_payloads[name]
        if _file_digest(data) != digest:
            raise ValueError(f"backup digest mismatch: {name}")
        payloads[name] = data

    database_name = "workspace/job-scout.db"
    old_applications_root = Path(archived_applications_root).resolve()
    new_applications_root = paths.applications.resolve()
    database_payload = payloads.get(database_name)
    if database_payload is None:
        raise ValueError("backup has no SQLite database")
    with tempfile.TemporaryDirectory() as temporary_directory:
        staging_root = Path(temporary_directory)
        staged_database = staging_root / "job-scout.db"
        staged_database.write_bytes(database_payload)
        _validate_packet_payloads(
            staged_database,
            payloads=payloads,
            stored_applications_root=old_applications_root,
            staging_root=staging_root / "packet-validation",
        )
        _relocate_packet_paths(
            staged_database,
            old_applications_root=old_applications_root,
            new_applications_root=new_applications_root,
        )
        ok, detail = _sqlite_integrity(staged_database)
        if not ok:
            raise ValueError(f"restored database failed integrity check: {detail}")
        payloads[database_name] = staged_database.read_bytes()

    destinations = {
        name: _destination_for(paths, name) for name in payloads
    }
    if len(set(destinations.values())) != len(destinations):
        raise ValueError("duplicate canonical restore destination")
    for destination in destinations.values():
        _reject_symlink_ancestors(destination)
        if os.path.lexists(destination):
            raise FileExistsError(f"restore target exists: {destination}")
    installed: list[Path] = []
    temporaries: list[Path] = []
    created_directories: list[Path] = []
    try:
        for name, data in sorted(payloads.items()):
            destination = destinations[name]
            _reject_symlink_ancestors(destination)
            missing_directories: list[Path] = []
            parent = destination.parent
            while not parent.exists():
                missing_directories.append(parent)
                parent = parent.parent
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            created_directories.extend(reversed(missing_directories))
            temporary_handle = tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.restore-",
                delete=False,
            )
            temporary = Path(temporary_handle.name)
            temporaries.append(temporary)
            with temporary_handle:
                temporary_handle.write(data)
                temporary_handle.flush()
                os.fsync(temporary_handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            temporaries.remove(temporary)
            installed.append(destination)
        ok, detail = _sqlite_integrity(paths.database)
        if not ok:
            raise ValueError(f"restored database failed integrity check: {detail}")
    except Exception:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
        for destination in reversed(installed):
            destination.unlink(missing_ok=True)
        for directory in sorted(
            set(created_directories), key=lambda path: len(path.parts), reverse=True
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return manifest
