"""Template propagation checks, not semantic validation of generated resumes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkflowPolicyTests(unittest.TestCase):
    def test_example_attestations_require_candidate_confirmation(self):
        profile = json.loads((ROOT / "config/application-profile.example.json").read_text())
        self.assertIn("tailoring_policy", profile)
        self.assertEqual(profile["tailoring_policy"], {
            "master_resume_non_exhaustive": None,
            "confirmed_experience_domains": [],
            "allow_in_domain_contextual_inference": None,
            "technologies_confirmed_at_every_job": [],
            "employer_context": [],
            "contrary_facts": [],
        })
        guidance = (ROOT / "docs/USAGE.md").read_text()
        for key in profile["tailoring_policy"]:
            self.assertIn(f"`{key}`", guidance)
        self.assertIn("does not enforce semantic tailoring", guidance)
        self.assertIn("does not update an already-installed skill", guidance)

    def test_rendered_workflow_carries_drafting_privacy_and_revision_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workspace = home / "jobs"
            workspace.mkdir()
            profile = workspace / "search-profile.json"
            profile.write_text(json.dumps({"candidate": {"name": "Example Operator"}}))
            # Sentinel artifacts exercise rendering without touching real applications.
            preserved = [profile]
            for status in ("pending_approval", "approved", "submitted"):
                packet = workspace / "applications" / status
                packet.mkdir(parents=True)
                artifact = packet / "resume.tex"
                artifact.write_text(f"Existing {status} artifact; employment dates: Present")
                preserved.append(artifact)
            application_profile = workspace / "application-profile.json"
            application_profile.write_text('{"private_confirmation": "preserve"}')
            preserved.append(application_profile)
            before = {path: path.read_bytes() for path in preserved}
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("JOB_SEARCH_", "RESUME_", "TECTONIC", "HERMES_"))
            }
            hermes_home = home / "hermes"
            environment.update({
                "HOME": str(home),
                "JOB_SEARCH_AUTOMATION": str(ROOT),
                "JOB_SEARCH_WORKDIR": str(workspace),
                "JOB_SEARCH_HERMES_HOME": str(hermes_home),
                "RESUME_DIR": str(home / "resume"),
                "MASTER_RESUME_FILE": str(home / "resume/master-resume.tex"),
                "TECTONIC": str(home / "bin/tectonic"),
            })
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/render_runtime.py"), "--home", str(home)],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, {path: path.read_bytes() for path in preserved})
            skill = (hermes_home / "skills/productivity/continuous-job-search/SKILL.md").read_text()
            prompt = (workspace / "cron-prompt.md").read_text()
            for text in (skill, prompt):
                with self.subTest(template="skill" if text == skill else "prompt"):
                    self.assertNotIn("{{", text)
                    self.assertIn("Example Operator", text)
                    for rule in (
                        "application-profile.json", "tailoring_policy",
                        "add, rewrite, combine, and reorder", "every substantive",
                        "employer-contextual inference", "exact years", "metrics",
                        "cover letters", "pay-range acceptance", "minimum rates", "C2C/1099",
                        "retain current employment", "exactly `N/A`", "date-only",
                        "Present", "future drafts/revisions", "approved", "submitted",
                        "`closed`", "`closure_reason: superseded`", "not `skipped`",
                    ):
                        self.assertIn(rule, text, rule)
            for rule in (
                "non-exhaustive", "keyword-by-keyword confirmation",
                "contrary facts", "implausible chronology", "unrelated domains",
                "architectures", "specific accomplishments", "clearance",
                "inference internally", "experience-section diff", "fit-based justification",
                "technologies_confirmed_at_every_job", "null", "not permission",
            ):
                self.assertIn(rule, skill, rule)
            self.assertNotIn("Use only the configured profile and master resume.", skill)


if __name__ == "__main__":
    unittest.main()
