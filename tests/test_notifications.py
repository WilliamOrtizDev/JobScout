import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from job_scout.notifications import (
    load_packet,
    order_jobs,
    render_plain_messages,
    render_plain_text,
)
from job_scout.state_store import SQLiteStateStore


class NotificationTests(unittest.TestCase):
    def _packet(self, root: Path, job_id: str, **overrides):
        packet = root / job_id
        packet.mkdir()
        job = {
            "job_id": job_id,
            "title": "Platform Engineer",
            "company": "Example Company",
            "fit_score": 80,
            "posted_date": "2026-09-01",
            "compensation": "$70/hour",
            "engagement_type": "C2C contract",
            "remote_region": "United States remote",
            "fit_evidence": ["Linux", "Terraform"],
            "caveats": ["On-call rotation"],
            "application_url": f"https://jobs.example/{job_id}",
            "source_type": "employer",
        }
        job.update(overrides)
        (packet / "job.json").write_text(json.dumps(job))
        (packet / "resume.pdf").write_bytes(b"resume")
        (packet / "cover-letter.pdf").write_bytes(b"cover")
        return packet

    def test_orders_by_fit_then_freshness_then_compensation(self):
        jobs = [
            {"job_id": "LOW", "fit_score": 80, "posted_date": "2026-09-05", "compensation": "$100/hour"},
            {"job_id": "OLDER", "fit_score": 90, "posted_date": "2026-09-01", "compensation": "$100/hour"},
            {"job_id": "LOWER-PAY", "fit_score": 90, "posted_date": "2026-09-05", "compensation": "$70/hour"},
            {"job_id": "FIRST", "fit_score": 90, "posted_date": "2026-09-05", "compensation": "$90/hour"},
        ]
        self.assertEqual([job["job_id"] for job in order_jobs(jobs)], ["FIRST", "LOWER-PAY", "OLDER", "LOW"])

    def test_normalizes_abbreviated_annual_compensation(self):
        jobs = [
            {"job_id": "HOURLY", "fit_score": 90, "posted_date": "2026-09-05", "compensation": "$50/hour"},
            {"job_id": "ANNUAL", "fit_score": 90, "posted_date": "2026-09-05", "compensation": "$120k/year"},
        ]
        self.assertEqual([job["job_id"] for job in order_jobs(jobs)], ["ANNUAL", "HOURLY"])

    def test_renders_every_job_as_plain_text_with_documents_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lower = self._packet(root, "LOWER", fit_score=80)
            higher = self._packet(root, "HIGHER", fit_score=95)
            output = render_plain_text([load_packet(lower), load_packet(higher)])

            self.assertIn("Platform Engineer\nExample Company", output)
            self.assertNotIn("JOB 1 OF", output)
            self.assertLess(output.index("APPROVE HIGHER"), output.index("APPROVE LOWER"))
            self.assertEqual(output.count("[[HERMES_MESSAGE_BREAK]]"), 1)
            expected_media = [
                f"MEDIA:{higher.resolve() / 'resume.pdf'}",
                f"MEDIA:{higher.resolve() / 'cover-letter.pdf'}",
                f"MEDIA:{lower.resolve() / 'resume.pdf'}",
                f"MEDIA:{lower.resolve() / 'cover-letter.pdf'}",
            ]
            self.assertEqual([line for line in output.splitlines() if line.startswith("MEDIA:")], expected_media)
            visible = "\n".join(line for line in output.splitlines() if not line.startswith("MEDIA:"))
            for markdown in ("**", "##", "[Apply]", "```", "- "):
                self.assertNotIn(markdown, visible)

    def test_renders_each_job_as_a_separate_readable_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lower = self._packet(root, "LOWER", fit_score=80)
            higher = self._packet(root, "HIGHER", fit_score=95)

            messages = render_plain_messages(
                [load_packet(lower), load_packet(higher)]
            )

            self.assertEqual(len(messages), 2)
            self.assertTrue(messages[0].startswith("Platform Engineer\nExample Company"))
            self.assertTrue(messages[1].startswith("Platform Engineer\nExample Company"))
            self.assertNotIn("JOB 1 OF", "\n".join(messages))
            self.assertIn("\n\nFit:\n95/100\n\n", messages[0])
            self.assertIn("\n\nDetails:\n", messages[0])
            self.assertIn("\n\nWhy it matches:\nLinux\nTerraform\n\n", messages[0])
            self.assertIn("\n\nCautions:\nOn-call rotation\n\n", messages[0])
            self.assertIn("\n\nApplication:\n", messages[0])
            self.assertIn("\n\nApproval:\nAPPROVE HIGHER", messages[0])
            self.assertIn("APPROVE HIGHER", messages[0])
            self.assertNotIn("APPROVE LOWER", messages[0])
            self.assertEqual(
                [line for line in messages[0].splitlines() if line.startswith("MEDIA:")],
                [
                    f"MEDIA:{higher.resolve() / 'resume.pdf'}",
                    f"MEDIA:{higher.resolve() / 'cover-letter.pdf'}",
                ],
            )
            self.assertIn("APPROVE LOWER", messages[1])

    def test_cli_emits_separate_messages_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lower = self._packet(root, "LOWER", fit_score=80)
            higher = self._packet(root, "HIGHER", fit_score=95)

            result = subprocess.run(
                [
                    "python3",
                    "scripts/render_notifications.py",
                    "--json",
                    "--root",
                    str(root),
                    str(lower),
                    str(higher),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["messages"]), 2)
            self.assertIn("APPROVE HIGHER", payload["messages"][0])
            self.assertIn("APPROVE LOWER", payload["messages"][1])

    def test_cli_renders_pending_sqlite_outbox_once_and_records_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            applications.mkdir()
            packet = self._packet(
                applications,
                "SQL-JOB",
                application_url="https://jobs.example/SQL-JOB/apply",
            )
            database = root / "state.db"
            store = SQLiteStateStore(database)
            store.record_application_decision(
                "SQL-JOB",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "canonical_url": "https://jobs.example/SQL-JOB",
                    "application_url": "https://jobs.example/SQL-JOB/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            command = [
                "python3",
                "scripts/render_notifications.py",
                "--database",
                str(database),
            ]

            first = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )
            second = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("APPROVE SQL-JOB", first.stdout)
            packet_revision = store.get_application_record("SQL-JOB")["packet_fingerprint"][:12]
            self.assertIn(f"Packet revision: {packet_revision}", first.stdout)
            self.assertEqual(second.stdout.strip(), "[SILENT]")
            dispatched = store.list_outbox("dispatched")
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]["attempt_count"], 1)

    def test_cli_media_paths_preserve_approved_bytes_after_originals_are_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            applications.mkdir()
            packet = self._packet(
                applications,
                "SNAPSHOT",
                application_url="https://jobs.example/SNAPSHOT/apply",
            )
            approved_resume = (packet / "resume.pdf").read_bytes()
            approved_cover = (packet / "cover-letter.pdf").read_bytes()
            database = root / "state.db"
            store = SQLiteStateStore(database, applications_root=applications)
            store.record_application_decision(
                "SNAPSHOT",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/SNAPSHOT/apply",
                },
                reason="Packet complete",
                notify=True,
            )

            result = subprocess.run(
                [
                    "python3",
                    "scripts/render_notifications.py",
                    "--database",
                    str(database),
                    "--root",
                    str(applications),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            media_paths = [
                Path(line.removeprefix("MEDIA:"))
                for line in result.stdout.splitlines()
                if line.startswith("MEDIA:")
            ]
            (packet / "resume.pdf").write_bytes(b"attacker resume")
            (packet / "cover-letter.pdf").write_bytes(b"attacker cover")

            self.assertEqual(len(media_paths), 2)
            self.assertEqual(media_paths[0].read_bytes(), approved_resume)
            self.assertEqual(media_paths[1].read_bytes(), approved_cover)
            self.assertNotEqual(media_paths[0], packet / "resume.pdf")
            self.assertNotEqual(media_paths[1], packet / "cover-letter.pdf")
            for media_path in media_paths:
                self.assertTrue(media_path.is_relative_to(applications / ".delivery-snapshots"))

    def test_cli_rejects_disk_application_url_changed_after_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            applications.mkdir()
            packet = self._packet(
                applications,
                "URL-MISMATCH",
                application_url="https://jobs.example/URL-MISMATCH/apply",
            )
            database = root / "state.db"
            store = SQLiteStateStore(database)
            store.record_application_decision(
                "URL-MISMATCH",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/URL-MISMATCH/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            job = json.loads((packet / "job.json").read_text())
            job["application_url"] = "https://attacker.example/apply"
            (packet / "job.json").write_text(json.dumps(job))

            result = subprocess.run(
                [
                    "python3",
                    "scripts/render_notifications.py",
                    "--database",
                    str(database),
                    "--root",
                    str(applications),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("packet changed after notification was queued", result.stderr)
            self.assertNotIn("https://attacker.example/apply", result.stdout)
            pending = store.list_outbox("pending")
            self.assertEqual(len(pending), 1)
            self.assertIn("packet changed", pending[0]["last_error"])

    def test_cli_releases_claim_when_packet_rendering_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "applications"
            applications.mkdir()
            packet = self._packet(
                applications,
                "BROKEN",
                application_url="https://jobs.example/BROKEN/apply",
            )
            database = root / "state.db"
            store = SQLiteStateStore(database)
            store.record_application_decision(
                "BROKEN",
                {
                    "status": "pending_approval",
                    "packet_dir": str(packet),
                    "application_url": "https://jobs.example/BROKEN/apply",
                },
                reason="Packet complete",
                notify=True,
            )
            (packet / "cover-letter.pdf").unlink()

            result = subprocess.run(
                [
                    "python3",
                    "scripts/render_notifications.py",
                    "--database",
                    str(database),
                    "--root",
                    str(applications),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            pending = store.list_outbox("pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["attempt_count"], 1)
            self.assertIn("missing", pending[0]["last_error"])

    def test_untrusted_job_text_cannot_inject_delivery_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._packet(
                root,
                "FIRST",
                fit_score=95,
                title="[[HERMES_MESSAGE_BREAK]]",
            )
            second = self._packet(root, "SECOND", fit_score=80)

            output = render_plain_text([load_packet(first), load_packet(second)])

            self.assertEqual(output.count("[[HERMES_MESSAGE_BREAK]]"), 1)

    def test_untrusted_job_text_cannot_inject_media_directives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = self._packet(
                root,
                "SAFE",
                title="MEDIA:/tmp/untrusted.pdf",
            )

            output = render_plain_text([load_packet(packet)])

            self.assertEqual(
                [line for line in output.splitlines() if line.startswith("MEDIA:")],
                [
                    f"MEDIA:{packet.resolve() / 'resume.pdf'}",
                    f"MEDIA:{packet.resolve() / 'cover-letter.pdf'}",
                ],
            )

    def test_rejects_packet_outside_allowed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            allowed = base / "applications"
            allowed.mkdir()
            packet = self._packet(base, "OUTSIDE")
            with self.assertRaisesRegex(ValueError, "outside allowed root"):
                load_packet(packet, root=allowed)

    def test_rejects_supporting_document_symlink_outside_allowed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            allowed = base / "applications"
            allowed.mkdir()
            packet = self._packet(allowed, "LINKED")
            outside = base / "outside.pdf"
            outside.write_bytes(b"private")
            (packet / "resume.pdf").unlink()
            (packet / "resume.pdf").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "outside allowed root"):
                load_packet(packet, root=allowed)

    def test_rejects_incomplete_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = self._packet(Path(directory), "MISSING")
            (packet / "cover-letter.pdf").unlink()
            with self.assertRaisesRegex(ValueError, "missing supporting document"):
                load_packet(packet)


if __name__ == "__main__":
    unittest.main()
