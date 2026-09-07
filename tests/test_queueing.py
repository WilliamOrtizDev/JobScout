from pathlib import Path
import tempfile
import unittest

from job_scout.queueing import rotating_batch
from job_scout.state_store import SQLiteStateStore


class QueueingTests(unittest.TestCase):
    def test_rotates_with_sqlite_cursor_without_a_state_file(self):
        jobs = [
            {"source": "lever", "tenant": "acme", "source_id": str(index)}
            for index in range(38)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStateStore(root / "state.db")
            first = rotating_batch(
                jobs, limit=25, cursor_store=store, queue_name="live-scout"
            )
            second = rotating_batch(
                jobs, limit=25, cursor_store=store, queue_name="live-scout"
            )

            self.assertFalse((root / "cursor.json").exists())

        self.assertEqual(
            {job["source_id"] for job in first + second},
            {str(index) for index in range(38)},
        )

    def test_rotates_overflow_so_every_lead_reaches_the_orchestrator(self):
        jobs = [
            {"source": "lever", "tenant": "acme", "source_id": str(index)}
            for index in range(38)
        ]
        with tempfile.TemporaryDirectory() as directory:
            cursor = Path(directory) / "cursor.json"
            first = rotating_batch(jobs, limit=25, cursor_path=cursor)
            second = rotating_batch(jobs, limit=25, cursor_path=cursor)

        first_ids = {job["source_id"] for job in first}
        second_ids = {job["source_id"] for job in second}
        self.assertEqual(len(first), 25)
        self.assertEqual(len(second), 25)
        self.assertEqual(first_ids | second_ids, {str(index) for index in range(38)})


if __name__ == "__main__":
    unittest.main()