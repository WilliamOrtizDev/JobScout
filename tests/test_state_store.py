import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from job_scout.models import Candidate
from job_scout.pipeline import PipelineResult
from job_scout.state_store import SQLiteStateStore
from scripts import collect as collect_script
from scripts import install_state as install_state_script


class SQLiteStateStoreTests(unittest.TestCase):
    def test_creates_complete_lifecycle_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")

            with sqlite3.connect(store.path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertGreaterEqual(version, 4)
            self.assertTrue(
                {
                    "application_events",
                    "delivery_outbox",
                    "queue_cursors",
                    "verification_attempts",
                    "approvals",
                    "submission_attempts",
                }.issubset(tables)
            )
            self.assertEqual(store.stats()["verification_attempts"], 0)
            self.assertEqual(store.stats()["approvals"], 0)
            self.assertEqual(store.stats()["submission_attempts"], 0)
            with sqlite3.connect(store.path) as connection:
                outbox_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(delivery_outbox)")
                }
            self.assertTrue(
                {
                    "packet_fingerprint",
                    "application_url",
                    "claim_token",
                    "claimed_at",
                    "lease_expires_at",
                }.issubset(outbox_columns)
            )

    def test_v4_migration_backfills_pending_outbox_revision_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "MIGRATE"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": "MIGRATE",
                        "application_url": "https://jobs.example/migrate/apply",
                    }
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            database = root / "state.db"
            store = SQLiteStateStore(database)
            store.record_application_decision(
                "MIGRATE",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/migrate/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            historical_payload = json.dumps(
                {"job_id": "MIGRATE", "packet_dir": str(packet), "audit": "keep-exact"}
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE application_records SET payload_json = "
                    "json_set(payload_json, '$.packet_fingerprint', 'legacy-v3-hash') "
                    "WHERE job_id = 'MIGRATE'"
                )
                connection.execute(
                    "UPDATE delivery_outbox SET packet_fingerprint = NULL, "
                    "application_url = NULL, payload_json = ?",
                    (historical_payload,),
                )
                connection.execute("PRAGMA user_version = 3")

            migrated_store = SQLiteStateStore(database)
            migrated = migrated_store.list_outbox("pending")[0]
            historical = migrated_store.list_outbox("invalidated")[0]

            self.assertIsNone(historical["packet_fingerprint"])
            self.assertIsNone(historical["application_url"])
            self.assertEqual(historical["payload"], json.loads(historical_payload))
            self.assertNotEqual(migrated["packet_fingerprint"], "legacy-v3-hash")
            self.assertEqual(
                migrated["payload"]["packet_fingerprint"],
                migrated["packet_fingerprint"],
            )
            self.assertEqual(
                migrated["payload"]["application_url"], migrated["application_url"]
            )

    def test_v4_migration_creates_photon_notification_without_historical_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "NO-OUTBOX"
            packet.mkdir(parents=True)
            application_url = "https://jobs.example/no-outbox/apply"
            (packet / "job.json").write_text(
                json.dumps({"job_id": "NO-OUTBOX", "application_url": application_url})
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            database = root / "state.db"
            store = SQLiteStateStore(database)
            store.record_application_decision(
                "NO-OUTBOX",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": application_url,
                },
                reason="Packet complete",
                notify=True,
            )
            with sqlite3.connect(database) as connection:
                connection.execute("DELETE FROM delivery_outbox WHERE job_id = 'NO-OUTBOX'")
                connection.execute(
                    "UPDATE application_records SET status = 'approved', "
                    "payload_json = json_set(payload_json, '$.status', 'approved') "
                    "WHERE job_id = 'NO-OUTBOX'"
                )
                connection.execute("PRAGMA user_version = 3")

            migrated = SQLiteStateStore(database)

            self.assertEqual(migrated.get_application_record("NO-OUTBOX")["status"], "pending_approval")
            pending = migrated.list_outbox("pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["transport"], "photon")
            self.assertEqual(pending[0]["payload"]["packet_dir"], str(packet.resolve()))
            self.assertEqual(pending[0]["application_url"], application_url)

    def test_v4_migration_rejects_unbound_pending_approval_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "UNBOUND"
            packet.mkdir(parents=True)
            application_url = "https://jobs.example/unbound/apply"
            (packet / "job.json").write_text(
                json.dumps({"job_id": "UNBOUND", "application_url": application_url})
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            database = root / "state.db"
            store = SQLiteStateStore(database)
            store.record_application_decision(
                "UNBOUND",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": application_url,
                },
                reason="Packet complete",
                notify=True,
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE application_records SET payload_json = "
                    "json_remove(payload_json, '$.packet_fingerprint') "
                    "WHERE job_id = 'UNBOUND'"
                )
                connection.execute(
                    "UPDATE delivery_outbox SET packet_fingerprint = NULL"
                )
                connection.execute("PRAGMA user_version = 3")
            (packet / "resume.pdf").unlink()

            with self.assertRaisesRegex(
                RuntimeError,
                "cannot migrate approval packet revision",
            ):
                SQLiteStateStore(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_v4_migration_invalidates_pre_v4_approvals_and_requires_fresh_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            store = SQLiteStateStore(database)
            outbox_ids = {}
            for job_id in ("APPROVED", "BLOCKED", "SUBMITTED"):
                packet = root / "applications" / job_id
                packet.mkdir(parents=True)
                application_url = f"https://jobs.example/{job_id}/apply"
                (packet / "job.json").write_text(
                    json.dumps({"job_id": job_id, "application_url": application_url})
                )
                (packet / "resume.pdf").write_bytes(b"resume")
                (packet / "cover-letter.pdf").write_bytes(b"cover")
                store.record_application_decision(
                    job_id,
                    {
                        "status": "pending_approval",
                        "packet_dir": str(packet),
                        "application_url": application_url,
                    },
                    reason="Packet complete",
                    notify=True,
                )
                outbox = store.list_outbox("pending")[0]
                outbox_ids[job_id] = outbox["outbox_id"]
                store.mark_outbox_dispatched([outbox["outbox_id"]])
                store.approve_application(job_id)

            self.assertTrue(store.mark_outbox_delivered(outbox_ids["BLOCKED"]))
            self.assertTrue(store.mark_outbox_delivered(outbox_ids["SUBMITTED"]))
            store.record_submission("BLOCKED", outcome="blocked", error="MFA required")
            store.record_submission(
                "SUBMITTED",
                outcome="submitted",
                confirmation={"application_id": "APP-1"},
            )
            store.record_application_decision(
                "CLOSED",
                {
                    "status": "closed",
                    "application_url": "https://jobs.example/CLOSED/apply",
                },
                reason="Listing closed",
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO delivery_outbox (
                        job_id, fingerprint, transport, payload_json, status,
                        attempt_count, created_at, updated_at, delivered_at,
                        last_error, packet_fingerprint, application_url,
                        claim_token, claimed_at, lease_expires_at
                    )
                    SELECT job_id, fingerprint || ':duplicate', transport, payload_json,
                           'delivered', 7, created_at, updated_at, delivered_at,
                           'historical retry', packet_fingerprint, application_url,
                           NULL, NULL, NULL
                    FROM delivery_outbox WHERE job_id = 'BLOCKED'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO delivery_outbox (
                        job_id, fingerprint, transport, payload_json, status,
                        attempt_count, created_at, updated_at, delivered_at,
                        last_error, packet_fingerprint, application_url,
                        claim_token, claimed_at, lease_expires_at
                    ) VALUES (
                        'CLOSED', 'closed-history', 'photon', '{}', 'delivered',
                        3, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                        '2026-01-01T00:00:00Z', NULL, 'historical',
                        'https://jobs.example/CLOSED/apply', NULL, NULL, NULL
                    )
                    """
                )
                connection.execute("PRAGMA user_version = 3")

            migrated = SQLiteStateStore(database)

            self.assertEqual(migrated.get_application_record("APPROVED")["status"], "pending_approval")
            self.assertEqual(migrated.get_application_record("BLOCKED")["status"], "pending_approval")
            self.assertEqual(migrated.get_application_record("SUBMITTED")["status"], "submitted")
            migration_reason = (
                "Schema v4 invalidated legacy approval proof; fresh notification and approval required"
            )
            self.assertIn(
                migration_reason,
                [event["reason"] for event in migrated.get_application_record("APPROVED")["events"]],
            )
            self.assertIn(
                migration_reason,
                [event["reason"] for event in migrated.get_application_record("BLOCKED")["events"]],
            )
            self.assertNotIn(
                migration_reason,
                [event["reason"] for event in migrated.get_application_record("SUBMITTED")["events"]],
            )
            pending_rows = migrated.list_outbox("pending")
            self.assertEqual(len(pending_rows), 2)
            pending = {item["job_id"]: item for item in pending_rows}
            self.assertEqual(set(pending), {"APPROVED", "BLOCKED"})
            for job_id, item in pending.items():
                self.assertNotEqual(item["outbox_id"], outbox_ids[job_id])
                self.assertEqual(item["attempt_count"], 0)
                self.assertIsNone(item["delivered_at"])
                self.assertEqual(
                    item["payload"]["packet_fingerprint"], item["packet_fingerprint"]
                )
                self.assertEqual(item["payload"]["application_url"], item["application_url"])
            invalidated_rows = migrated.list_outbox("invalidated")
            invalidated_by_id = {item["outbox_id"]: item for item in invalidated_rows}
            self.assertEqual(
                [item["job_id"] for item in invalidated_rows].count("BLOCKED"), 2
            )
            self.assertEqual(
                {item["job_id"] for item in invalidated_rows},
                {"APPROVED", "BLOCKED"},
            )
            approved_history = invalidated_by_id[outbox_ids["APPROVED"]]
            self.assertEqual(approved_history["attempt_count"], 1)
            self.assertIsNone(approved_history["delivered_at"])
            blocked_history = invalidated_by_id[outbox_ids["BLOCKED"]]
            self.assertEqual(blocked_history["attempt_count"], 1)
            self.assertIsNotNone(blocked_history["delivered_at"])
            self.assertEqual(
                sorted(
                    item["attempt_count"]
                    for item in invalidated_rows
                    if item["job_id"] == "BLOCKED"
                ),
                [1, 7],
            )
            self.assertEqual(
                {item["job_id"] for item in migrated.list_outbox("delivered")},
                {"CLOSED", "SUBMITTED"},
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM approvals WHERE status = 'active'"
                    ).fetchone()[0],
                    0,
                )

    def test_rejects_out_of_root_and_symlinked_packet_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            applications.mkdir()
            outside = root / "OUTSIDE"
            outside.mkdir()
            for packet in (outside,):
                (packet / "job.json").write_text("{}")
                (packet / "resume.pdf").write_bytes(b"resume")
                (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db", applications_root=applications)
            record = {
                "status": "pending_approval",
                "packet_dir": str(outside),
                "application_url": "https://jobs.example/outside/apply",
            }

            with self.assertRaisesRegex(ValueError, "outside applications root"):
                store.record_application_decision(
                    "OUTSIDE", record, reason="Packet complete", notify=True
                )

            real = applications / "REAL"
            real.mkdir()
            (real / "job.json").write_text("{}")
            (real / "resume.pdf").write_bytes(b"resume")
            (real / "cover-letter.pdf").write_bytes(b"cover")
            linked = applications / "LINKED"
            linked.symlink_to(real, target_is_directory=True)
            record["packet_dir"] = str(linked)
            with self.assertRaisesRegex(ValueError, "symlink"):
                store.record_application_decision(
                    "LINKED", record, reason="Packet complete", notify=True
                )

    def test_non_pending_decision_atomically_invalidates_live_notification_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "CANCEL"
            packet.mkdir(parents=True)
            application_url = "https://jobs.example/CANCEL/apply"
            (packet / "job.json").write_text(
                json.dumps({"job_id": "CANCEL", "application_url": application_url})
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db")
            store.record_application_decision(
                "CANCEL",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": application_url,
                },
                reason="Packet complete",
                notify=True,
            )
            claim = store.claim_outbox()
            self.assertEqual(len(claim["items"]), 1)

            store.record_application_decision(
                "CANCEL",
                {"status": "rejected"},
                reason="Role no longer suitable",
            )

            invalidated = store.list_outbox("invalidated")
            self.assertEqual(len(invalidated), 1)
            self.assertIsNone(invalidated[0]["claim_token"])
            self.assertIsNone(invalidated[0]["claimed_at"])
            self.assertIsNone(invalidated[0]["lease_expires_at"])
            self.assertEqual(store.claim_outbox()["items"], [])

    def test_pending_notification_persists_canonical_packet_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            packet = applications / "NORMALIZED"
            packet.mkdir(parents=True)
            application_url = "https://jobs.example/NORMALIZED/apply"
            (packet / "job.json").write_text(
                json.dumps({"job_id": "NORMALIZED", "application_url": application_url})
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db", applications_root=applications)

            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                store.record_application_decision(
                    "NORMALIZED",
                    {
                        "status": "pending_approval",
                        "packet_dir": "applications/../applications/NORMALIZED/.",
                        "application_url": application_url,
                    },
                    reason="Packet complete",
                    notify=True,
                )
            finally:
                os.chdir(old_cwd)

            canonical = str(packet.resolve())
            self.assertEqual(store.get_application_record("NORMALIZED")["packet_dir"], canonical)
            pending = store.list_outbox("pending")[0]
            self.assertEqual(pending["payload"]["packet_dir"], canonical)

    def test_packet_path_is_canonical_and_confined_without_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            packet = applications / "PACKET-READY"
            packet.mkdir(parents=True)
            store = SQLiteStateStore(root / "state.db", applications_root=applications)

            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                store.record_application_decision(
                    "PACKET-READY",
                    {
                        "status": "packet_ready",
                        "packet_dir": "applications/../applications/PACKET-READY/.",
                        "application_url": "https://jobs.example/PACKET-READY/apply",
                    },
                    reason="Packet assembly started",
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(
                store.get_application_record("PACKET-READY")["packet_dir"],
                str(packet.resolve()),
            )
            self.assertEqual(store.list_outbox("pending"), [])

    def test_rejects_packet_metadata_for_a_different_job_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "RIGHT"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(json.dumps({"job_id": "WRONG"}))
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db")

            with self.assertRaisesRegex(ValueError, "job_id does not match"):
                store.record_application_decision(
                    "RIGHT",
                    {
                        "job_id": "WRONG",
                        "status": "pending_approval",
                        "packet_dir": str(packet),
                        "application_url": "https://jobs.example/right/apply",
                    },
                    reason="Packet complete",
                    notify=True,
                )

    def test_rejects_pending_notification_when_job_json_url_differs_from_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "URL-MISMATCH"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": "URL-MISMATCH",
                        "application_url": "https://attacker.example/apply",
                    }
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db")

            with self.assertRaisesRegex(
                ValueError, "job.json application URL does not match application record"
            ):
                store.record_application_decision(
                    "URL-MISMATCH",
                    {
                        "status": "pending_approval",
                        "packet_dir": str(packet),
                        "application_url": "https://jobs.example/real/apply",
                    },
                    reason="Packet complete",
                    notify=True,
                )

            self.assertIsNone(store.get_application_record("URL-MISMATCH"))
            self.assertEqual(store.list_outbox("pending"), [])

    def test_records_application_decision_and_notification_transactionally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStateStore(root / "state.db")
            packet = root / "applications" / "JOB-1"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {"job_id": "JOB-1", "application_url": "https://jobs.example/1/apply"}
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            record = {
                "company": "Acme",
                "title": "Platform Engineer",
                "canonical_url": "https://jobs.example/1",
                "application_url": "https://jobs.example/1/apply",
                "status": "pending_approval",
                "packet_dir": str(packet),
                "fit_score": 88,
            }

            store.record_application_decision(
                "JOB-1",
                record,
                reason="Packet compiled",
                notify=True,
                verification={
                    "outcome": "verified",
                    "source_url": "https://jobs.example/1",
                    "evidence": {"remote": True, "contract": True},
                },
            )
            store.record_application_decision(
                "JOB-1", record, reason="Packet compiled", notify=True
            )

            saved = store.get_application_record("JOB-1")
            self.assertEqual(saved["status"], "pending_approval")
            self.assertEqual(saved["events"][0]["reason"], "Packet compiled")
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM delivery_outbox WHERE job_id = 'JOB-1'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM application_events WHERE job_id = 'JOB-1'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM verification_attempts WHERE job_id = 'JOB-1'"
                    ).fetchone()[0],
                    1,
                )

    def test_records_verification_evidence_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.record_application_decision(
                "VERIFY-1",
                {
                    "status": "verified",
                    "canonical_url": "https://jobs.example/verify",
                },
                reason="Listing verified",
            )

            verification_id = store.record_verification(
                "VERIFY-1",
                outcome="verified",
                source_url="https://jobs.example/verify",
                evidence={"remote": True, "contract": True},
            )

            self.assertGreater(verification_id, 0)
            with sqlite3.connect(store.path) as connection:
                saved = connection.execute(
                    "SELECT outcome, source_url, evidence_json FROM verification_attempts"
                ).fetchone()
            self.assertEqual(saved[0], "verified")
            self.assertEqual(saved[1], "https://jobs.example/verify")
            self.assertEqual(json.loads(saved[2]), {"contract": True, "remote": True})

    def test_approval_requires_dispatched_immutable_packet_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            applications.mkdir()
            original = applications / "JOB-IMMUTABLE"
            original.mkdir()
            (original / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": "JOB-IMMUTABLE",
                        "application_url": "https://jobs.example/v1/apply",
                    }
                )
            )
            (original / "resume.pdf").write_bytes(b"resume-v1")
            (original / "cover-letter.pdf").write_bytes(b"cover-v1")
            store = SQLiteStateStore(root / "state.db")
            store.record_application_decision(
                "JOB-IMMUTABLE",
                {
                    "status": "pending_approval",
                    "packet_dir": str(original),
                    "application_url": "https://jobs.example/v1/apply",
                },
                reason="Packet complete",
                notify=True,
            )

            with self.assertRaisesRegex(ValueError, "notification has not been dispatched"):
                store.approve_application("JOB-IMMUTABLE")

            revised = applications / "JOB-IMMUTABLE-R2"
            revised.mkdir()
            (revised / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": "JOB-IMMUTABLE",
                        "application_url": "https://jobs.example/v2/apply",
                    }
                )
            )
            (revised / "resume.pdf").write_bytes(b"resume-v2")
            (revised / "cover-letter.pdf").write_bytes(b"cover-v2")
            with self.assertRaisesRegex(ValueError, "new job revision ID"):
                store.record_application_decision(
                    "JOB-IMMUTABLE",
                    {
                        "status": "pending_approval",
                        "packet_dir": str(revised),
                        "application_url": "https://jobs.example/v2/apply",
                    },
                    reason="Revised packet",
                    notify=True,
                )

            pending = store.list_outbox("pending")
            store.mark_outbox_dispatched([pending[0]["outbox_id"]])
            approval_id = store.approve_application("JOB-IMMUTABLE")
            self.assertGreater(approval_id, 0)

    def test_approval_and_submission_are_bound_to_exact_packet_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "JOB-EXACT"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": "JOB-EXACT",
                        "application_url": "https://jobs.example/exact/apply",
                    }
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume-v1")
            (packet / "cover-letter.pdf").write_bytes(b"cover-v1")
            store = SQLiteStateStore(root / "state.db")
            store.record_application_decision(
                "JOB-EXACT",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/exact/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            pending = store.list_outbox("pending")
            store.mark_outbox_dispatched([pending[0]["outbox_id"]])

            approval_id = store.approve_application("JOB-EXACT")
            saved = store.get_application_record("JOB-EXACT")
            self.assertEqual(saved["status"], "approved")
            self.assertRegex(saved["packet_fingerprint"], r"^[0-9a-f]{64}$")

            (packet / "resume.pdf").write_bytes(b"resume-changed")
            with self.assertRaisesRegex(ValueError, "packet changed after approval"):
                store.record_submission(
                    "JOB-EXACT", outcome="submitted", confirmation={"id": "APP-1"}
                )

            (packet / "resume.pdf").write_bytes(b"resume-v1")
            (packet / "submission-confirmation.json").write_text(
                json.dumps({"id": "APP-1"})
            )
            submission_id = store.record_submission(
                "JOB-EXACT", outcome="submitted", confirmation={"id": "APP-1"}
            )
            self.assertGreater(approval_id, 0)
            self.assertGreater(submission_id, 0)
            self.assertEqual(store.get_application_record("JOB-EXACT")["status"], "submitted")
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0],
                    1,
                )

    def test_blocked_submission_can_resume_with_same_exact_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "JOB-BLOCKED"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": "JOB-BLOCKED",
                        "application_url": "https://jobs.example/blocked/apply",
                    }
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db")
            store.record_application_decision(
                "JOB-BLOCKED",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/blocked/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            pending = store.list_outbox("pending")
            store.mark_outbox_dispatched([pending[0]["outbox_id"]])
            store.approve_application("JOB-BLOCKED")
            store.record_submission(
                "JOB-BLOCKED", outcome="blocked", error="MFA required"
            )

            store.record_submission(
                "JOB-BLOCKED",
                outcome="submitted",
                confirmation={"application_id": "APP-BLOCKED"},
            )

            self.assertEqual(
                store.get_application_record("JOB-BLOCKED")["status"], "submitted"
            )
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0],
                    2,
                )

    def test_application_decisions_cannot_manufacture_approval_or_submission_states(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")

            for status in ("approved", "blocked", "submitted"):
                with self.subTest(status=status):
                    with self.assertRaisesRegex(
                        ValueError, "status is controlled by the approval lifecycle"
                    ):
                        store.record_application_decision(
                            f"FORGED-{status}",
                            {"status": status},
                            reason="Forged lifecycle state",
                        )
                    self.assertIsNone(store.get_application_record(f"FORGED-{status}"))

    def test_successful_submission_consumes_approval_and_cannot_be_replayed_or_regressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "JOB-ONCE"
            packet.mkdir(parents=True)
            application_url = "https://jobs.example/once/apply"
            (packet / "job.json").write_text(
                json.dumps({"job_id": "JOB-ONCE", "application_url": application_url})
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            store = SQLiteStateStore(root / "state.db")
            store.record_application_decision(
                "JOB-ONCE",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": application_url,
                },
                reason="Packet complete",
                notify=True,
            )
            pending = store.list_outbox("pending")
            store.mark_outbox_dispatched([pending[0]["outbox_id"]])
            approval_id = store.approve_application("JOB-ONCE")
            store.record_submission("JOB-ONCE", outcome="blocked", error="MFA required")
            store.record_submission(
                "JOB-ONCE",
                outcome="submitted",
                confirmation={"application_id": "APP-ONCE"},
            )

            with sqlite3.connect(store.path) as connection:
                approval = connection.execute(
                    "SELECT status, consumed_at FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                self.assertEqual(approval[0], "consumed")
                self.assertIsNotNone(approval[1])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO submission_attempts "
                        "(job_id, approval_id, attempted_at, outcome, confirmation_json) "
                        "VALUES (?, ?, ?, 'submitted', ?)",
                        (
                            "JOB-ONCE",
                            approval_id,
                            "2026-09-06T00:00:00Z",
                            json.dumps({"application_id": "APP-REPLAY"}),
                        ),
                    )
            with self.assertRaisesRegex(ValueError, "submitted application cannot regress"):
                store.record_application_decision(
                    "JOB-ONCE",
                    {
                        "status": "pending_approval",
                        "packet_dir": str(packet),
                        "application_url": application_url,
                    },
                    reason="Accidental replay",
                )
            with self.assertRaisesRegex(ValueError, "active exact-job approval"):
                store.record_submission(
                    "JOB-ONCE",
                    outcome="submitted",
                    confirmation={"application_id": "APP-REPLAY"},
                )

    def test_outbox_claim_is_atomic_and_failed_render_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "CLAIM"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {"job_id": "CLAIM", "application_url": "https://jobs.example/claim/apply"}
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            first = SQLiteStateStore(root / "state.db")
            first.record_application_decision(
                "CLAIM",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/claim/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            second = SQLiteStateStore(root / "state.db")

            claimed = first.claim_outbox(limit=10, lease_seconds=60)
            competing = second.claim_outbox(limit=10, lease_seconds=60)

            self.assertEqual(len(claimed["items"]), 1)
            self.assertEqual(competing["items"], [])
            self.assertTrue(
                first.release_outbox_claim(claimed["claim_token"], "render failed")
            )
            retried = second.claim_outbox(limit=10, lease_seconds=60)
            self.assertEqual(len(retried["items"]), 1)
            self.assertEqual(retried["items"][0]["attempt_count"], 2)
            self.assertEqual(first.mark_outbox_claim_dispatched(retried["claim_token"]), 1)
            self.assertEqual(len(first.list_outbox("dispatched")), 1)

    def test_tracks_delivery_success_failure_and_retry_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            for job_id in ("DELIVERED", "FAILED"):
                packet = Path(directory) / "applications" / job_id
                packet.mkdir(parents=True)
                (packet / "job.json").write_text(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "application_url": f"https://jobs.example/{job_id}/apply",
                        }
                    )
                )
                (packet / "resume.pdf").write_bytes(b"resume")
                (packet / "cover-letter.pdf").write_bytes(b"cover")
                store.record_application_decision(
                    job_id,
                    {
                        "status": "pending_approval",
                        "packet_dir": str(packet),
                        "application_url": f"https://jobs.example/{job_id}/apply",
                    },
                    reason="Packet complete",
                    notify=True,
                )
            pending = store.list_outbox("pending")
            by_job = {item["job_id"]: item for item in pending}
            store.mark_outbox_dispatched([item["outbox_id"] for item in pending])

            store.mark_outbox_delivered(by_job["DELIVERED"]["outbox_id"])
            store.mark_outbox_failed(by_job["FAILED"]["outbox_id"], "Photon timeout")

            delivered = store.list_outbox("delivered")
            failed = store.list_outbox("failed")
            self.assertIsNotNone(delivered[0]["delivered_at"])
            self.assertEqual(failed[0]["last_error"], "Photon timeout")
            self.assertTrue(store.retry_outbox(failed[0]["outbox_id"]))
            retried = store.list_outbox("pending")[0]
            self.assertEqual(retried["attempt_count"], 1)
            self.assertIsNone(retried["last_error"])

    def test_legacy_import_is_one_time_and_cannot_overwrite_sqlite_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "jobs": {
                            "JOB-1": {
                                "company": "Acme",
                                "title": "SRE",
                                "canonical_url": "https://jobs.example/1",
                                "status": "rejected",
                                "events": [
                                    {
                                        "at": "2026-09-05T12:00:00Z",
                                        "status": "rejected",
                                        "reason": "Below minimum compensation",
                                    }
                                ],
                            }
                        }
                    }
                )
            )
            store = SQLiteStateStore(root / "state.db")

            store.import_legacy_json(legacy)
            with self.assertRaisesRegex(ValueError, "empty SQLite lifecycle"):
                store.import_legacy_json(legacy)
            record = store.get_application_record("JOB-1")

            self.assertEqual(record["company"], "Acme")
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["events"][0]["reason"], "Below minimum compensation")
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM application_events WHERE job_id = 'JOB-1'"
                    ).fetchone()[0],
                    1,
                )

    def test_install_state_migrates_legacy_once_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "job-scout.db"
            legacy = root / "state.json"
            legacy.write_text(json.dumps({"jobs": {"OLD": {"status": "closed"}}}))
            command = [
                sys.executable,
                "scripts/install_state.py",
                "--database",
                str(database),
                "--legacy",
                str(legacy),
            ]

            migrated = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertFalse(legacy.exists())
            archive = Path(json.loads(migrated.stdout)["legacy_archive"])
            self.assertTrue(archive.is_file())
            self.assertEqual(SQLiteStateStore(database).get_application_record("OLD")["status"], "closed")

            legacy.write_text(json.dumps({"jobs": {"NEW": {"status": "rejected"}}}))
            refused = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(legacy.exists())
            self.assertIsNone(SQLiteStateStore(database).get_application_record("NEW"))

    def test_install_state_preserves_replacement_created_after_atomic_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "job-scout.db"
            legacy = root / "state.json"
            original = json.dumps({"jobs": {"OLD": {"status": "closed"}}}).encode()
            replacement = json.dumps({"jobs": {"NEW": {"status": "rejected"}}}).encode()
            legacy.write_bytes(original)
            real_archive = install_state_script._durable_archive

            def replace_then_archive(snapshot, database_path):
                legacy.write_bytes(replacement)
                return real_archive(snapshot, database_path)

            with patch.object(
                install_state_script,
                "_durable_archive",
                side_effect=replace_then_archive,
            ), self.assertRaisesRegex(ValueError, "replacement"):
                install_state_script.install_legacy_state(database, legacy)

            self.assertEqual(legacy.read_bytes(), replacement)
            archive = next((root / "legacy-archive").glob("state.*.json"))
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(
                SQLiteStateStore(database).get_application_record("OLD")["status"], "closed"
            )
            self.assertIsNone(SQLiteStateStore(database).get_application_record("NEW"))

    def test_install_state_rejects_symlink_legacy_without_moving_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "job-scout.db"
            target = root / "actual-state.json"
            target_bytes = json.dumps({"jobs": {"OLD": {"status": "closed"}}}).encode()
            target.write_bytes(target_bytes)
            legacy = root / "state.json"
            legacy.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "regular file"):
                install_state_script.install_legacy_state(database, legacy)

            self.assertTrue(legacy.is_symlink())
            self.assertEqual(target.read_bytes(), target_bytes)
            self.assertIsNone(SQLiteStateStore(database).get_application_record("OLD"))

    def test_install_state_recovers_after_import_commit_before_legacy_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "job-scout.db"
            legacy = root / "state.json"
            legacy.write_text(json.dumps({"jobs": {"OLD": {"status": "closed"}}}))
            archive_dir = root / "legacy-archive"
            archive_dir.mkdir()
            archive = archive_dir / "state.pre-crash.json"
            archive.write_bytes(legacy.read_bytes())
            SQLiteStateStore(database).import_legacy_json(legacy)

            recovered = subprocess.run(
                [
                    sys.executable,
                    "scripts/install_state.py",
                    "--database",
                    str(database),
                    "--legacy",
                    str(legacy),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertFalse(legacy.exists())
            payload = json.loads(recovered.stdout)
            self.assertTrue(payload["recovered"])
            self.assertEqual(Path(payload["legacy_archive"]), archive.resolve())
            self.assertEqual(
                SQLiteStateStore(database).get_application_record("OLD")["status"],
                "closed",
            )

    def test_install_state_recovery_rejects_different_legacy_content_at_same_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "job-scout.db"
            legacy = root / "state.json"
            legacy.write_text(json.dumps({"jobs": {"OLD": {"status": "closed"}}}))
            archive_dir = root / "legacy-archive"
            archive_dir.mkdir()
            (archive_dir / "state.pre-crash.json").write_bytes(legacy.read_bytes())
            SQLiteStateStore(database).import_legacy_json(legacy)
            legacy.write_text(json.dumps({"jobs": {"NEW": {"status": "rejected"}}}))
            (archive_dir / "state.replaced.json").write_bytes(legacy.read_bytes())

            refused = subprocess.run(
                [
                    sys.executable,
                    "scripts/install_state.py",
                    "--database",
                    str(database),
                    "--legacy",
                    str(legacy),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(legacy.exists())
            self.assertIn("does not match committed import", refused.stderr)
            self.assertIsNone(SQLiteStateStore(database).get_application_record("NEW"))

    def test_state_database_cli_supports_import_status_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            database = root / "state.db"
            exported = root / "rollback.json"
            legacy.write_text(json.dumps({"jobs": {"JOB-1": {"status": "closed"}}}))
            command = [sys.executable, "scripts/state_db.py", "--database", str(database)]

            imported = subprocess.run(
                [*command, "import", str(legacy)],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            status = subprocess.run(
                [*command, "status"],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [*command, "export", str(exported)],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(json.loads(imported.stdout)["imported"], 1)
            self.assertEqual(json.loads(status.stdout)["application_records"], 1)
            self.assertEqual(json.loads(exported.read_text())["jobs"]["JOB-1"]["status"], "closed")

    def test_state_database_cli_records_and_queries_lifecycle_without_json_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            record_path = root / "decision.json"
            packet = root / "applications" / "JOB-2"
            packet.mkdir(parents=True)
            (packet / "job.json").write_text(
                json.dumps(
                    {"job_id": "JOB-2", "application_url": "https://jobs.example/2/apply"}
                )
            )
            (packet / "resume.pdf").write_bytes(b"resume")
            (packet / "cover-letter.pdf").write_bytes(b"cover")
            record_path.write_text(
                json.dumps(
                    {
                        "company": "Acme",
                        "title": "SRE",
                        "canonical_url": "https://jobs.example/2",
                        "application_url": "https://jobs.example/2/apply",
                        "status": "pending_approval",
                        "packet_dir": str(packet),
                    }
                )
            )
            verification_path = root / "verification.json"
            verification_path.write_text(
                json.dumps(
                    {
                        "outcome": "verified",
                        "source_url": "https://jobs.example/2",
                        "evidence": {"remote": True, "contract": True},
                    }
                )
            )
            command = [sys.executable, "scripts/state_db.py", "--database", str(database)]

            subprocess.run(
                [
                    *command,
                    "record",
                    "JOB-2",
                    "--record",
                    str(record_path),
                    "--reason",
                    "Packet compiled",
                    "--notify",
                    "--verification",
                    str(verification_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            shown = subprocess.run(
                [*command, "show", "JOB-2"],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            outbox = subprocess.run(
                [*command, "outbox", "--status", "pending"],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(json.loads(shown.stdout)["status"], "pending_approval")
            outbox_payload = json.loads(outbox.stdout)
            self.assertEqual(outbox_payload["items"][0]["job_id"], "JOB-2")
            outbox_id = str(outbox_payload["items"][0]["outbox_id"])
            failed = subprocess.run(
                [*command, "outbox-failed", outbox_id, "--error", "delivery test"],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(failed.stdout)["updated"])
            self.assertEqual(
                SQLiteStateStore(database).list_outbox("failed")[0]["last_error"],
                "delivery test",
            )
            store = SQLiteStateStore(database)
            self.assertTrue(store.retry_outbox(int(outbox_id)))
            pending = store.list_outbox("pending")
            store.mark_outbox_dispatched([pending[0]["outbox_id"]])
            approved = subprocess.run(
                [*command, "approve", "JOB-2"],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            confirmation = root / "confirmation.json"
            confirmation.write_text(json.dumps({"application_id": "APP-2"}))
            submitted = subprocess.run(
                [
                    *command,
                    "submit",
                    "JOB-2",
                    "--outcome",
                    "submitted",
                    "--confirmation",
                    str(confirmation),
                ],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertGreater(json.loads(approved.stdout)["approval_id"], 0)
            self.assertGreater(json.loads(submitted.stdout)["submission_id"], 0)
            self.assertEqual(SQLiteStateStore(database).get_application_record("JOB-2")["status"], "submitted")
            self.assertEqual(SQLiteStateStore(database).stats()["verification_attempts"], 1)

    def test_database_is_private_on_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"

            SQLiteStateStore(database)

            self.assertEqual(os.stat(database).st_mode & 0o777, 0o600)

    def test_repairs_an_interrupted_version_zero_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE application_records (
                        job_id TEXT PRIMARY KEY,
                        canonical_url TEXT,
                        application_url TEXT,
                        content_hash TEXT,
                        status TEXT,
                        payload_json TEXT NOT NULL,
                        imported_at TEXT NOT NULL
                    )
                    """
                )

            store = SQLiteStateStore(database)

            self.assertEqual(store.stats()["schema_version"], 4)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='collector_runs'"
                    ).fetchone()[0],
                    1,
                )

    def test_run_lock_serializes_collector_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            acquired = threading.Event()

            def wait_for_lock():
                with store.run_lock():
                    acquired.set()

            with store.run_lock():
                thread = threading.Thread(target=wait_for_lock)
                thread.start()
                time.sleep(0.05)
                self.assertFalse(acquired.is_set())
            thread.join(timeout=1)
            self.assertTrue(acquired.is_set())

    def test_run_lock_rejects_symlink_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStateStore(root / "state.db")
            victim = root / "victim"
            victim.write_text("preserve")
            victim.chmod(0o644)
            lock = root / "state.db.run.lock"
            lock.symlink_to(victim)

            with self.assertRaises(OSError):
                with store.run_lock():
                    pass

            self.assertEqual(victim.read_text(), "preserve")
            self.assertEqual(victim.stat().st_mode & 0o777, 0o644)

    def test_rejects_list_legacy_state_that_cannot_round_trip_faithfully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            legacy.write_text(json.dumps({"jobs": [{"status": "closed"}]}))
            store = SQLiteStateStore(root / "state.db")

            with self.assertRaisesRegex(ValueError, "object keyed by stable job IDs"):
                store.import_legacy_json(legacy)

    def test_creates_versioned_database_and_imports_legacy_dict_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            database = root / "state.db"
            legacy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": "2026-09-05T00:00:00Z",
                        "jobs": {
                            "JOB-1": {
                                "canonical_url": "https://jobs.example/1",
                                "application_url": "https://jobs.example/1/apply",
                                "content_hash": "hash-1",
                                "status": "pending_approval",
                                "events": [{"status": "pending_approval"}],
                            }
                        },
                    }
                )
            )

            store = SQLiteStateStore(database)
            imported = store.import_legacy_json(legacy)

            self.assertEqual(imported, 1)
            self.assertEqual(
                store.known_records(),
                {
                    "https://jobs.example/1": "hash-1",
                    "https://jobs.example/1/apply": "hash-1",
                },
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                payload = connection.execute(
                    "SELECT payload_json FROM application_records WHERE job_id = 'JOB-1'"
                ).fetchone()[0]
            self.assertEqual(json.loads(payload)["status"], "pending_approval")

    def test_exports_imported_records_for_json_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            exported = root / "exported.json"
            record = {
                "canonical_url": "https://jobs.example/rollback",
                "status": "blocked",
                "events": [{"status": "blocked", "reason": "test"}],
            }
            legacy.write_text(json.dumps({"jobs": {"JOB-R": record}}))
            store = SQLiteStateStore(root / "state.db")
            store.import_legacy_json(legacy)

            exported_count = store.export_legacy_json(exported)

            self.assertEqual(exported_count, 1)
            self.assertEqual(json.loads(exported.read_text())["jobs"], {"JOB-R": record})

    def test_records_each_collection_run_and_upserts_candidate_history(self):
        candidate = Candidate(
            source="lever",
            tenant="acme",
            source_id="role-1",
            company="Acme",
            title="Platform Engineer",
            location="Remote",
            description="Linux contract",
            employment_type="Contract",
            remote=True,
            posted_at="2026-09-05T00:00:00Z",
            canonical_url="https://jobs.example/role-1",
            apply_url="https://jobs.example/role-1/apply",
            raw={"ignored": True},
        )
        result = PipelineResult(
            jobs=[candidate],
            source_failures=[],
            rejected_counts={"onsite": 2},
            configured_sources=3,
            successful_sources=2,
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            store = SQLiteStateStore(database)
            run_id = store.record_pipeline_result(result)

            self.assertEqual(
                store.known_records()[candidate.apply_url],
                candidate.to_dict()["content_hash"],
            )
            with sqlite3.connect(database) as connection:
                run = connection.execute(
                    "SELECT accepted_count, configured_sources, successful_sources, rejected_json "
                    "FROM collector_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                saved = connection.execute(
                    "SELECT payload_json, first_seen_at, last_seen_at FROM candidates"
                ).fetchone()
            self.assertEqual(run[:3], (1, 3, 2))
            self.assertEqual(json.loads(run[3]), {"onsite": 2})
            self.assertNotIn("raw", json.loads(saved[0]))
            self.assertEqual(saved[1], saved[2])

    def test_unprocessed_candidates_do_not_enter_application_deduplication(self):
        candidate = Candidate(
            source="lever",
            tenant="acme",
            source_id="queued-role",
            company="Acme",
            title="Platform Engineer",
            location="Remote",
            description="Linux contract",
            employment_type="Contract",
            remote=True,
            posted_at="2026-09-05T00:00:00Z",
            canonical_url="https://jobs.example/queued-role",
            apply_url="https://jobs.example/queued-role/apply",
            raw={},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStateStore(root / "state.db")
            store.record_pipeline_result(PipelineResult([candidate], [], {}, 1, 1))

            self.assertEqual(store.application_dedupe_entries(), [])

            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "jobs": {
                            "APP-1": {
                                "canonical_url": candidate.canonical_url,
                                "content_hash": candidate.to_dict()["content_hash"],
                                "status": "pending_approval",
                            }
                        }
                    }
                )
            )
            store.import_legacy_json(state)
            self.assertEqual(
                store.application_dedupe_entries(),
                [(candidate.canonical_url, candidate.to_dict()["content_hash"])],
            )

    def test_incomplete_application_states_do_not_suppress_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "jobs": {
                            "DISCOVERED": {
                                "canonical_url": "https://jobs.example/discovered",
                                "status": "discovered",
                            },
                            "READY": {
                                "canonical_url": "https://jobs.example/ready",
                                "status": "pending_approval",
                            },
                        }
                    }
                )
            )
            store = SQLiteStateStore(root / "state.db")
            store.import_legacy_json(state)

            self.assertEqual(
                store.application_dedupe_entries(),
                [("https://jobs.example/ready", None)],
            )

    def test_legacy_application_without_hash_suppresses_matching_collector_candidate(self):
        candidate = Candidate(
            source="lever",
            tenant="acme",
            source_id="role-2",
            company="Acme",
            title="SRE",
            location="Remote",
            description="Contract Linux",
            employment_type="Contract",
            remote=True,
            posted_at=None,
            canonical_url="https://jobs.example/shared",
            apply_url="https://jobs.example/shared/apply",
            raw={},
        )
        result = PipelineResult([candidate], [], {}, 1, 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "jobs": {
                            "APP-1": {
                                "canonical_url": candidate.canonical_url,
                                "status": "pending_approval",
                            }
                        }
                    }
                )
            )
            store = SQLiteStateStore(root / "state.db")
            store.import_legacy_json(legacy)
            store.record_pipeline_result(result)

            self.assertIsNone(store.known_records()[candidate.canonical_url])

    def test_preserves_distinct_hashes_for_the_same_url_across_state_sources(self):
        candidate = Candidate(
            source="lever",
            tenant="acme",
            source_id="role-collision",
            company="Acme",
            title="SRE",
            location="Remote",
            description="Contract Linux",
            employment_type="Contract",
            remote=True,
            posted_at=None,
            canonical_url="https://jobs.example/collision",
            apply_url="https://jobs.example/collision/apply",
            raw={},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "jobs": {
                            "APP-1": {
                                "canonical_url": candidate.canonical_url,
                                "content_hash": "application-hash",
                            }
                        }
                    }
                )
            )
            store = SQLiteStateStore(root / "state.db")
            store.import_legacy_json(legacy)
            store.record_pipeline_result(PipelineResult([candidate], [], {}, 1, 1))

            matching = [
                value
                for url, value in store.known_record_entries()
                if url == candidate.canonical_url
            ]

            self.assertCountEqual(
                matching,
                ["application-hash", candidate.to_dict()["content_hash"]],
            )

    def test_collect_cli_reads_application_deduplication_from_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources.json"
            profile = root / "profile.json"
            database = root / "state.db"
            output = root / "candidates.json"
            sources.write_text(json.dumps({"lever": [{"tenant": "acme"}]}))
            profile.write_text(json.dumps({"targets": {}}))
            SQLiteStateStore(database).record_application_decision(
                "JOB-1",
                {
                    "canonical_url": "https://jobs.example/1",
                    "content_hash": "known-hash",
                    "status": "closed",
                },
                reason="Listing closed",
            )
            result = PipelineResult([], [], {}, 1, 1)
            argv = [
                "collect.py",
                "--sources",
                str(sources),
                "--profile",
                str(profile),
                "--database",
                str(database),
                "--output",
                str(output),
            ]

            with patch.object(sys, "argv", argv), patch.object(
                collect_script, "run_pipeline", return_value=result
            ) as run:
                self.assertEqual(collect_script.main(), 0)

            self.assertEqual(
                run.call_args.kwargs["known_records"],
                [("https://jobs.example/1", "known-hash")],
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
