import io
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from job_scout.portability import (
    RuntimePaths,
    backup_runtime,
    doctor,
    install_tectonic_archive,
    node_version_supported,
    render_template,
    restore_runtime,
    tectonic_release,
)
from job_scout.state_store import packet_fingerprint


def _sqlite_bytes(*, packet_dir=None, packet_hash=None):
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE application_records (job_id TEXT, payload_json TEXT)"
            )
            if packet_dir is not None:
                connection.execute(
                    "INSERT INTO application_records VALUES ('JOB-1', ?)",
                    (json.dumps({"packet_dir": packet_dir, "packet_fingerprint": packet_hash}),),
                )
        return database.read_bytes()


def _write_backup(archive, files, *, applications_root="/old/applications", tar_format=None):
    manifest = {
        "schema_version": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "source_commit": "test",
        "applications_root": applications_root,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    kwargs = {} if tar_format is None else {"format": tar_format}
    with tarfile.open(archive, "w:gz", **kwargs) as bundle:
        entries = [("manifest.json", json.dumps(manifest).encode()), *files.items()]
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if tar_format == tarfile.PAX_FORMAT and name == "manifest.json":
                member.pax_headers = {"comment": "extension metadata"}
            bundle.addfile(member, io.BytesIO(data))
    return manifest


class PortabilityTests(unittest.TestCase):
    def test_photon_node_version_requires_18_17_or_newer(self):
        self.assertFalse(node_version_supported("v18.16.1"))
        self.assertTrue(node_version_supported("v18.17.0"))
        self.assertTrue(node_version_supported("v20.12.2"))
        self.assertFalse(node_version_supported("unexpected"))

    def test_selects_pinned_tectonic_release_for_linux_architectures(self):
        x86 = tectonic_release("Linux", "x86_64")
        arm = tectonic_release("Linux", "aarch64")

        self.assertIn("tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz", x86.url)
        self.assertEqual(
            x86.sha256,
            "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7",
        )
        self.assertIn("tectonic-0.17.0-aarch64-unknown-linux-musl.tar.gz", arm.url)

    def test_rejects_unsupported_tectonic_platform(self):
        with self.assertRaisesRegex(ValueError, "unsupported platform"):
            tectonic_release("Windows", "AMD64")

    def test_installs_only_verified_tectonic_binary_from_archive(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
            binary = b"tectonic executable"
            member = tarfile.TarInfo("tectonic")
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
        archive = payload.getvalue()

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bin/tectonic"
            install_tectonic_archive(
                archive,
                expected_sha256=hashlib.sha256(archive).hexdigest(),
                destination=destination,
            )
            installed = destination.read_bytes()
            executable = bool(destination.stat().st_mode & stat.S_IXUSR)

        self.assertEqual(installed, b"tectonic executable")
        self.assertTrue(executable)

    def test_rejects_tectonic_archive_with_wrong_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "checksum"):
                install_tectonic_archive(
                    b"not trusted",
                    expected_sha256="0" * 64,
                    destination=Path(directory) / "tectonic",
                )

    def test_runtime_template_requires_all_placeholders(self):
        rendered = render_template(
            "Workspace={{WORKSPACE}} Automation={{AUTOMATION}}",
            {"WORKSPACE": "/srv/jobs", "AUTOMATION": "/srv/repo"},
        )
        self.assertEqual(rendered, "Workspace=/srv/jobs Automation=/srv/repo")

        with self.assertRaisesRegex(ValueError, "unresolved template placeholder"):
            render_template("{{MISSING}}", {})

    def test_runtime_paths_honor_all_location_overrides(self):
        root = Path("/checkout")
        home = Path("/home/example")
        paths = RuntimePaths.from_environment(
            root,
            home=home,
            environment={
                "HERMES_HOME": "/srv/hermes",
                "JOB_SEARCH_HERMES_HOME": "/srv/hermes/profiles/job-scout",
                "JOB_SEARCH_WORKDIR": "/srv/job-search",
                "RESUME_DIR": "/srv/resume",
                "JOB_SEARCH_AUTOMATION": "/srv/automation",
            },
        )

        self.assertEqual(paths.hermes_home, Path("/srv/hermes/profiles/job-scout"))
        self.assertEqual(paths.workspace, Path("/srv/job-search"))
        self.assertEqual(paths.resume_dir, Path("/srv/resume"))
        self.assertEqual(paths.automation, Path("/srv/automation"))
        self.assertEqual(paths.database, Path("/srv/job-search/job-scout.db"))

    def test_runtime_paths_expand_home_relative_values(self):
        paths = RuntimePaths.from_environment(
            Path("/checkout"),
            home=Path("/home/example"),
            environment={"JOB_SEARCH_WORKDIR": "~/private/jobs"},
        )

        self.assertEqual(paths.workspace, Path("/home/example/private/jobs"))

    def test_runtime_paths_use_the_only_existing_resume_for_legacy_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            resume_dir = home / "job-search-resume"
            resume_dir.mkdir()
            legacy = resume_dir / "Candidate_Resume.tex"
            legacy.write_text("resume")

            paths = RuntimePaths.from_environment(
                home / "JobScout", home=home, environment={}
            )

        self.assertEqual(paths.resume, legacy)

    def test_doctor_reports_required_and_optional_capabilities_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(
                base / "repo", home=base, environment={}
            )
            paths.workspace.mkdir(parents=True)
            paths.resume_dir.mkdir(parents=True)
            paths.hermes_home.joinpath("skills/productivity/continuous-job-search").mkdir(
                parents=True
            )
            paths.search_profile.write_text('{"candidate": {"name": "Example"}}')
            paths.application_profile.write_text('{"status": "configured"}')
            paths.resume.write_text("resume")
            paths.skill.write_text("skill")
            local_tectonic = base / ".local/bin/tectonic"
            local_tectonic.parent.mkdir(parents=True)
            local_tectonic.write_text("binary")
            with sqlite3.connect(paths.database) as connection:
                connection.execute("CREATE TABLE smoke (id INTEGER)")

            checks = doctor(
                paths,
                command_lookup=lambda name: f"/usr/bin/{name}"
                if name in {"git", "curl", "python3", "hermes"}
                else None,
            )

        by_name = {check.name: check for check in checks}
        self.assertTrue(by_name["Hermes CLI"].ok)
        self.assertTrue(by_name["SQLite database"].ok)
        self.assertTrue(by_name["Tectonic"].ok)
        self.assertFalse(by_name["Photon/Node.js"].ok)
        self.assertFalse(by_name["Photon/Node.js"].required)

    def test_doctor_rejects_unconfigured_candidate_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(base / "repo", home=base, environment={})
            paths.workspace.mkdir(parents=True)
            paths.resume_dir.mkdir(parents=True)
            paths.skill.parent.mkdir(parents=True)
            paths.search_profile.write_text('{"candidate": {"name": "Candidate Name"}}')
            paths.application_profile.write_text('{"status": "configuration_required"}')
            paths.resume.write_text("resume")
            paths.skill.write_text("skill")
            with sqlite3.connect(paths.database) as connection:
                connection.execute("CREATE TABLE smoke (id INTEGER)")

            checks = doctor(
                paths,
                command_lookup=lambda name: f"/usr/bin/{name}",
            )

        by_name = {check.name: check for check in checks}
        self.assertFalse(by_name["Candidate profile configured"].ok)
        self.assertFalse(by_name["Application profile configured"].ok)
        self.assertTrue(by_name["Candidate profile configured"].required)

    def test_backup_is_consistent_and_excludes_caches_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(
                base / "repo", home=base, environment={}
            )
            paths.workspace.joinpath("applications/JOB-1").mkdir(parents=True)
            paths.workspace.joinpath(".http-cache").mkdir()
            paths.resume_dir.mkdir(parents=True)
            paths.search_profile.write_text('{"candidate": "Example"}')
            paths.application_profile.write_text('{"status": "configured"}')
            paths.workspace.joinpath("applications/JOB-1/resume.pdf").write_bytes(b"pdf")
            paths.workspace.joinpath(".http-cache/ignored").write_text("cache")
            paths.workspace.joinpath("credentials.json").write_text("secret")
            paths.resume.write_text("resume")
            with sqlite3.connect(paths.database) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE records (value TEXT)")
                connection.execute("INSERT INTO records VALUES ('kept')")
            archive = base / "backup.tar.gz"

            manifest = backup_runtime(paths, archive, source_commit="abc123")

            with tarfile.open(archive, "r:gz") as bundle:
                names = set(bundle.getnames())
                bundled_manifest = json.load(bundle.extractfile("manifest.json"))
                database_bytes = bundle.extractfile("workspace/job-scout.db").read()
            restored_db = base / "snapshot.db"
            restored_db.write_bytes(database_bytes)
            with sqlite3.connect(restored_db) as connection:
                value = connection.execute("SELECT value FROM records").fetchone()[0]

        self.assertEqual(manifest["source_commit"], "abc123")
        self.assertEqual(bundled_manifest["schema_version"], 1)
        self.assertEqual(value, "kept")
        self.assertIn("workspace/applications/JOB-1/resume.pdf", names)
        self.assertIn("resume/master-resume.tex", names)
        self.assertNotIn("workspace/.http-cache/ignored", names)
        self.assertNotIn("workspace/credentials.json", names)
        self.assertTrue(all(not name.startswith("/") and ".." not in Path(name).parts for name in names))

    def test_backup_refuses_archive_that_aliases_live_database(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(base / "repo", home=base, environment={})
            paths.workspace.mkdir(parents=True)
            with sqlite3.connect(paths.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")

            with self.assertRaisesRegex(ValueError, "outside runtime inputs"):
                backup_runtime(paths, paths.database)

            with sqlite3.connect(paths.database) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]

        self.assertEqual(integrity, "ok")

    def test_backup_rejects_symlinked_output_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(base / "repo", home=base, environment={})
            paths.workspace.mkdir(parents=True)
            with sqlite3.connect(paths.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")
            victim = base / "victim"
            victim.write_text("preserve")
            archive = base / "backup.tar.gz"
            archive.symlink_to(victim)

            with self.assertRaisesRegex(ValueError, "symlink"):
                backup_runtime(paths, archive)

            self.assertEqual(victim.read_text(), "preserve")

    def test_backup_rejects_filename_restore_would_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(base / "repo", home=base, environment={})
            packet = paths.applications / "JOB-1"
            packet.mkdir(parents=True)
            packet.joinpath("bad\\name.txt").write_text("bad")
            with sqlite3.connect(paths.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")

            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                backup_runtime(paths, base / "backup.tar.gz")

    def test_restore_refuses_nonempty_destination_and_restores_verified_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = RuntimePaths.from_environment(
                base / "source-repo", home=base / "source-home", environment={}
            )
            source.workspace.mkdir(parents=True)
            source.resume_dir.mkdir(parents=True)
            source.search_profile.write_text("{}")
            source.application_profile.write_text("{}")
            source.resume.write_text("resume")
            with sqlite3.connect(source.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")
            archive = base / "backup.tar.gz"
            backup_runtime(source, archive, source_commit="abc123")

            destination_home = base / "destination"
            destination = RuntimePaths.from_environment(
                base / "destination-repo", home=destination_home, environment={}
            )
            destination.workspace.mkdir(parents=True)
            destination.workspace.joinpath("existing").write_text("keep")
            with self.assertRaisesRegex(FileExistsError, "not empty"):
                restore_runtime(archive, destination)

            destination.workspace.joinpath("existing").unlink()
            result = restore_runtime(archive, destination)

            with sqlite3.connect(destination.database) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            search_profile_exists = destination.search_profile.exists()
            resume_exists = destination.resume.exists()

        self.assertEqual(result["source_commit"], "abc123")
        self.assertEqual(integrity, "ok")
        self.assertTrue(search_profile_exists)
        self.assertTrue(resume_exists)

    def test_restore_rejects_archive_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "malicious.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                payload = b"bad"
                member = tarfile.TarInfo("../escape")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
            destination = RuntimePaths.from_environment(
                base / "repo", home=base / "home", environment={}
            )

            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                restore_runtime(archive, destination)

        self.assertFalse((base / "escape").exists())

    def test_restore_rejects_archive_without_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "missing-manifest.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                payload = b"database"
                member = tarfile.TarInfo("workspace/job-scout.db")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
            destination = RuntimePaths.from_environment(
                base / "repo", home=base / "home", environment={}
            )

            with self.assertRaisesRegex(ValueError, "no manifest"):
                restore_runtime(archive, destination)

    def test_restore_rejects_oversized_archive_member_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "oversized.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                payload = b"x" * 1024
                member = tarfile.TarInfo("workspace/job-scout.db")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
            destination = RuntimePaths.from_environment(
                base / "repo", home=base / "home", environment={}
            )

            with mock.patch("job_scout.portability.MAX_BACKUP_FILE_BYTES", 512):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    restore_runtime(archive, destination)

    def test_restore_rejects_duplicate_archive_member_names(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "duplicate.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for payload in (b"first", b"second"):
                    member = tarfile.TarInfo("workspace/job-scout.db")
                    member.size = len(payload)
                    bundle.addfile(member, io.BytesIO(payload))
            destination = RuntimePaths.from_environment(
                base / "repo", home=base / "home", environment={}
            )

            with self.assertRaisesRegex(ValueError, "duplicate archive member"):
                restore_runtime(archive, destination)

    def test_restore_refuses_existing_private_source_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = RuntimePaths.from_environment(
                base / "source-repo", home=base / "source-home", environment={}
            )
            source.workspace.mkdir(parents=True)
            source.resume_dir.mkdir(parents=True)
            source.source_registry.parent.mkdir(parents=True)
            source.source_registry.write_text("{}")
            with sqlite3.connect(source.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")
            archive = base / "backup.tar.gz"
            backup_runtime(source, archive)
            destination = RuntimePaths.from_environment(
                base / "destination-repo",
                home=base / "destination-home",
                environment={},
            )
            destination.source_registry.parent.mkdir(parents=True)
            destination.source_registry.write_text('{"preserve": true}')

            with self.assertRaisesRegex(FileExistsError, "restore target exists"):
                restore_runtime(archive, destination)

            preserved = destination.source_registry.read_text()

        self.assertEqual(preserved, '{"preserve": true}')

    def test_restore_rolls_back_files_when_commit_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = RuntimePaths.from_environment(
                base / "source-repo", home=base / "source-home", environment={}
            )
            source.workspace.mkdir(parents=True)
            source.resume_dir.mkdir(parents=True)
            source.search_profile.write_text("{}")
            source.resume.write_text("resume")
            packet = source.applications / "JOB-1"
            packet.mkdir(parents=True)
            packet.joinpath("job.json").write_text("{}")
            with sqlite3.connect(source.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")
            archive = base / "backup.tar.gz"
            backup_runtime(source, archive)
            destination = RuntimePaths.from_environment(
                base / "destination-repo",
                home=base / "destination-home",
                environment={},
            )
            original_replace = os.replace

            def fail_resume(path, target):
                if Path(target).name == "search-profile.json":
                    raise OSError("simulated restore failure")
                return original_replace(path, target)

            with mock.patch("job_scout.portability.os.replace", fail_resume):
                with self.assertRaisesRegex(OSError, "simulated restore failure"):
                    restore_runtime(archive, destination)

            installed_files = [
                path
                for root in (destination.workspace, destination.resume_dir)
                if root.exists()
                for path in root.rglob("*")
                if path.is_file()
            ]
            restore_runtime(archive, destination)
            with sqlite3.connect(destination.database) as connection:
                retry_integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]

        self.assertEqual(installed_files, [])
        self.assertEqual(retry_integrity, "ok")

    def test_restore_relocates_packet_paths_in_sqlite_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = RuntimePaths.from_environment(
                base / "source-repo", home=base / "source-home", environment={}
            )
            packet = source.applications / "JOB-1"
            packet.mkdir(parents=True)
            packet.joinpath("job.json").write_text("{}")
            source.resume_dir.mkdir(parents=True)
            payload = json.dumps({"packet_dir": str(packet.resolve())})
            with sqlite3.connect(source.database) as connection:
                connection.execute(
                    "CREATE TABLE application_records (job_id TEXT, payload_json TEXT)"
                )
                connection.execute(
                    "CREATE TABLE delivery_outbox (outbox_id INTEGER, payload_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO application_records VALUES ('JOB-1', ?)", (payload,)
                )
                connection.execute(
                    "INSERT INTO delivery_outbox VALUES (1, ?)", (payload,)
                )
            archive = base / "backup.tar.gz"
            backup_runtime(source, archive)
            destination = RuntimePaths.from_environment(
                base / "destination-repo",
                home=base / "destination-home",
                environment={},
            )

            restore_runtime(archive, destination)

            with sqlite3.connect(destination.database) as connection:
                application_payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM application_records"
                    ).fetchone()[0]
                )
                outbox_payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM delivery_outbox"
                    ).fetchone()[0]
                )


        expected = str((destination.applications / "JOB-1").resolve())
        self.assertEqual(application_payload["packet_dir"], expected)
        self.assertEqual(outbox_payload["packet_dir"], expected)

    def test_restore_rejects_noncanonical_and_nonallowlisted_paths(self):
        cases = (
            "workspace//tmp/escape",
            "workspace/./job-scout.db",
            "workspace/other.txt",
            "resume/other.tex",
            "automation-private/nested/sources.json",
        )
        for unsafe_name in cases:
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                archive = base / "unsafe.tar.gz"
                files = {"workspace/job-scout.db": _sqlite_bytes(), unsafe_name: b"bad"}
                _write_backup(archive, files)
                paths = RuntimePaths.from_environment(base / "repo", home=base / "home", environment={})
                with self.assertRaisesRegex(ValueError, "unsafe|unsupported"):
                    restore_runtime(archive, paths)
        self.assertFalse(Path("/tmp/escape").exists())

    def test_restore_rejects_aliasing_canonical_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "aliases.tar.gz"
            _write_backup(
                archive,
                {
                    "workspace/job-scout.db": _sqlite_bytes(),
                    "workspace/./job-scout.db": _sqlite_bytes(),
                },
            )
            paths = RuntimePaths.from_environment(base / "repo", home=base / "home", environment={})
            with self.assertRaisesRegex(ValueError, "unsafe|duplicate"):
                restore_runtime(archive, paths)

    def test_restore_rejects_symlinked_destination_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "backup.tar.gz"
            files = {
                "workspace/job-scout.db": _sqlite_bytes(),
                "automation-private/sources.json": b"{}",
            }
            _write_backup(archive, files)
            paths = RuntimePaths.from_environment(base / "repo", home=base / "home", environment={})
            outside = base / "outside"
            outside.mkdir()
            paths.source_registry.parent.parent.mkdir(parents=True)
            paths.source_registry.parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                restore_runtime(archive, paths)
            self.assertEqual(list(outside.iterdir()), [])

    def test_restore_rejects_tar_extension_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "pax.tar.gz"
            _write_backup(
                archive,
                {"workspace/job-scout.db": _sqlite_bytes()},
                tar_format=tarfile.PAX_FORMAT,
            )
            paths = RuntimePaths.from_environment(base / "repo", home=base / "home", environment={})
            with self.assertRaisesRegex(ValueError, "extension"):
                restore_runtime(archive, paths)

    def test_backup_rejects_oversized_input_before_read_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = RuntimePaths.from_environment(base / "repo", home=base, environment={})
            paths.workspace.mkdir(parents=True)
            with sqlite3.connect(paths.database) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")
            archive = base / "backup.tar.gz"
            with mock.patch("job_scout.portability.MAX_BACKUP_FILE_BYTES", 1):
                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                    with self.assertRaisesRegex(ValueError, "size limit"):
                        backup_runtime(paths, archive)

    def test_restore_honors_custom_master_resume_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "backup.tar.gz"
            _write_backup(
                archive,
                {
                    "workspace/job-scout.db": _sqlite_bytes(),
                    "resume/master-resume.tex": b"custom resume",
                },
            )
            custom = base / "private" / "candidate.tex"
            paths = RuntimePaths.from_environment(
                base / "repo",
                home=base / "home",
                environment={"MASTER_RESUME_FILE": str(custom)},
            )
            restore_runtime(archive, paths)
            self.assertEqual(custom.read_bytes(), b"custom resume")
            self.assertFalse((paths.resume_dir / "master-resume.tex").exists())

    def test_backup_rejects_stale_packet_fingerprint_and_missing_required_file(self):
        for mutation in ("change", "missing"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                paths = RuntimePaths.from_environment(base / "repo", home=base, environment={})
                packet = paths.applications / "JOB-1"
                packet.mkdir(parents=True)
                for name, data in (("job.json", b"{}"), ("resume.pdf", b"resume"), ("cover-letter.pdf", b"cover")):
                    packet.joinpath(name).write_bytes(data)
                fingerprint = packet_fingerprint(packet, applications_root=paths.applications)
                with sqlite3.connect(paths.database) as connection:
                    connection.execute("CREATE TABLE application_records (job_id TEXT, payload_json TEXT)")
                    connection.execute(
                        "INSERT INTO application_records VALUES ('JOB-1', ?)",
                        (json.dumps({"packet_dir": str(packet), "packet_fingerprint": fingerprint}),),
                    )
                if mutation == "change":
                    packet.joinpath("resume.pdf").write_bytes(b"changed")
                else:
                    packet.joinpath("cover-letter.pdf").unlink()
                with self.assertRaisesRegex(ValueError, "fingerprint|missing"):
                    backup_runtime(paths, base / "backup.tar.gz")

    def test_restore_rejects_packet_bytes_that_mismatch_database_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            old_root = Path("/old/applications")
            packet_dir = old_root / "JOB-1"
            packet_files = {
                "workspace/applications/JOB-1/job.json": b"{}",
                "workspace/applications/JOB-1/resume.pdf": b"changed",
                "workspace/applications/JOB-1/cover-letter.pdf": b"cover",
            }
            archive = base / "tampered.tar.gz"
            _write_backup(
                archive,
                {
                    "workspace/job-scout.db": _sqlite_bytes(
                        packet_dir=str(packet_dir), packet_hash="0" * 64
                    ),
                    **packet_files,
                },
                applications_root=str(old_root),
            )
            paths = RuntimePaths.from_environment(base / "repo", home=base / "home", environment={})
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                restore_runtime(archive, paths)

    def test_restore_ignores_preexisting_predictable_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "backup.tar.gz"
            _write_backup(
                archive,
                {
                    "workspace/job-scout.db": _sqlite_bytes(),
                    "automation-private/sources.json": b'{"safe": true}',
                },
            )
            paths = RuntimePaths.from_environment(
                base / "repo", home=base / "home", environment={}
            )
            paths.source_registry.parent.mkdir(parents=True)
            victim = base / "victim"
            victim.write_text("preserve")
            predictable = paths.source_registry.with_name(
                f".{paths.source_registry.name}.restore-{os.getpid()}"
            )
            predictable.symlink_to(victim)

            restore_runtime(archive, paths)

            self.assertEqual(victim.read_text(), "preserve")
            self.assertFalse(paths.source_registry.is_symlink())
            self.assertEqual(paths.source_registry.read_text(), '{"safe": true}')

    def test_backup_rejects_symlinked_database_and_applications_roots(self):
        for source_name in ("database", "applications"):
            with self.subTest(source_name=source_name), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                paths = RuntimePaths.from_environment(
                    base / "repo", home=base / "home", environment={}
                )
                paths.workspace.mkdir(parents=True)
                outside = base / "outside"
                outside.mkdir()
                if source_name == "database":
                    outside_db = outside / "outside.db"
                    with sqlite3.connect(outside_db) as connection:
                        connection.execute("CREATE TABLE records (id INTEGER)")
                    paths.database.symlink_to(outside_db)
                else:
                    with sqlite3.connect(paths.database) as connection:
                        connection.execute("CREATE TABLE records (id INTEGER)")
                    outside.joinpath("secret.txt").write_text("do not include")
                    paths.applications.symlink_to(outside, target_is_directory=True)

                with self.assertRaisesRegex(ValueError, "symlink"):
                    backup_runtime(paths, base / "backup.tar.gz")

    def test_restore_requires_two_ustar_end_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "complete.tar.gz"
            _write_backup(
                archive,
                {"workspace/job-scout.db": _sqlite_bytes()},
            )
            with gzip.open(archive, "rb") as source:
                raw = source.read()
            offset = 0
            while raw[offset : offset + tarfile.BLOCKSIZE] != b"\0" * tarfile.BLOCKSIZE:
                member = tarfile.TarInfo.frombuf(
                    raw[offset : offset + tarfile.BLOCKSIZE],
                    encoding="utf-8",
                    errors="surrogateescape",
                )
                offset += tarfile.BLOCKSIZE + (
                    (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
                ) * tarfile.BLOCKSIZE
            truncated = base / "truncated.tar.gz"
            with gzip.open(truncated, "wb") as destination:
                destination.write(raw[:offset])
            paths = RuntimePaths.from_environment(
                base / "repo", home=base / "home", environment={}
            )

            with self.assertRaisesRegex(ValueError, "end markers|truncated"):
                restore_runtime(truncated, paths)


if __name__ == "__main__":
    unittest.main()
