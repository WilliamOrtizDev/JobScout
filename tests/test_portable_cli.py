import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/portable.py"


class PortableCliTests(unittest.TestCase):
    def test_doctor_json_succeeds_on_complete_minimal_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            for name in ("git", "curl", "python3", "hermes", "tectonic"):
                command = bin_dir / name
                command.write_text("#!/bin/sh\nexit 0\n")
                command.chmod(command.stat().st_mode | stat.S_IXUSR)
            workspace = base / "jobs"
            resume_dir = base / "resume"
            hermes_home = base / "hermes"
            skill = hermes_home / "skills/productivity/continuous-job-search/SKILL.md"
            workspace.mkdir()
            resume_dir.mkdir()
            skill.parent.mkdir(parents=True)
            (workspace / "search-profile.json").write_text(
                '{"candidate": {"name": "Example"}}'
            )
            (workspace / "application-profile.json").write_text(
                '{"status": "configured"}'
            )
            (resume_dir / "master-resume.tex").write_text("resume")
            skill.write_text("skill")
            with sqlite3.connect(workspace / "job-scout.db") as connection:
                connection.execute("CREATE TABLE smoke (id INTEGER)")
            environment = {
                **os.environ,
                "PATH": str(bin_dir),
                "HOME": str(base),
                "HERMES_HOME": str(hermes_home),
                "JOB_SEARCH_WORKDIR": str(workspace),
                "RESUME_DIR": str(resume_dir),
                "JOB_SEARCH_AUTOMATION": str(ROOT),
            }

            completed = subprocess.run(
                [sys.executable, str(CLI), "doctor", "--json"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ready"])
        self.assertTrue(any(row["name"] == "SQLite database" for row in payload["checks"]))

    def test_doctor_returns_nonzero_when_required_runtime_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "HOME": directory,
                "PATH": directory,
                "JOB_SEARCH_AUTOMATION": str(ROOT),
            }
            completed = subprocess.run(
                [sys.executable, str(CLI), "doctor", "--json"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertFalse(json.loads(completed.stdout)["ready"])


if __name__ == "__main__":
    unittest.main()
