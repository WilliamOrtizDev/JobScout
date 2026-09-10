import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "repository_validator", ROOT / "scripts/validate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class RepositoryValidationTests(unittest.TestCase):
    def test_example_profile_covers_security_role_families_without_physical_security(self):
        profile = json.loads((ROOT / "config/search-profile.example.json").read_text())
        titles = set(profile["targets"]["titles"])
        required = {
            "Information Systems Security Engineer",
            "Cybersecurity Engineer",
            "Cloud Security Engineer",
            "Security Automation Engineer",
            "Information Systems Security Officer",
            "Cybersecurity Consultant",
            "Cybersecurity Analyst",
            "RMF Engineer",
            "Information Assurance Engineer",
            "Security Control Assessor",
        }

        self.assertEqual(required - titles, set())
        self.assertNotIn("Security Officer", titles)

    def test_priority_boards_search_security_role_families(self):
        sources = json.loads((ROOT / "config/sources.example.json").read_text())
        required = {
            "information security",
            "cybersecurity",
            "information systems security officer",
            "information assurance",
            "rmf",
        }

        for source in ("dice", "linkedin"):
            with self.subTest(source=source):
                queries = set(sources[source][0]["queries"])
                self.assertEqual(required - queries, set())
                self.assertLessEqual(len(queries), 20)

    def test_jobscout_branding_replaces_legacy_project_name(self):
        legacy_slug = "job-search" + "-automation"
        legacy_phrases = (
            legacy_slug,
            "job-search" + " automation",
            "job search" + " automation",
        )
        legacy_remote = f"https://github.com/WilliamOrtizDev/{legacy_slug}"
        public_remote = "https://github.com/WilliamOrtizDev/" + "JobScout"
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode().split("\0")
        findings = []
        public_remote_locations = []
        for relative in filter(None, tracked):
            path = ROOT / relative
            if not path.is_file():
                continue
            try:
                content = path.read_text()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if public_remote.casefold() in line.casefold():
                    public_remote_locations.append(relative)
                if legacy_remote.casefold() in line.casefold() or any(
                    phrase in line.casefold() for phrase in legacy_phrases
                ):
                    findings.append(f"{relative}:{number}")
        self.assertEqual(findings, [])
        self.assertEqual(set(public_remote_locations), {"docs/GETTING_STARTED.md"})
        self.assertEqual(len(public_remote_locations), 2)
        self.assertTrue((ROOT / "README.md").read_text().startswith("# JobScout\n"))
        environment = (ROOT / ".env.example").read_text()
        self.assertIn("JOB_SEARCH_AUTOMATION=$HOME/JobScout", environment)
        self.assertIn("JOB_SEARCH_WORKDIR=$HOME/job-search", environment)

        getting_started = (ROOT / "docs/GETTING_STARTED.md").read_text()
        self.assertIn(f"git clone {public_remote}.git ~/JobScout", getting_started)
        migration = (ROOT / "docs/MIGRATION.md").read_text()
        self.assertNotIn('cd "$HOME/JobScout"', migration)
        self.assertNotIn('git -C "$HOME/JobScout"', migration)
        self.assertIn('cd "$JOB_SEARCH_AUTOMATION"', migration)

        help_result = subprocess.run(
            ["python3", str(ROOT / "scripts/portable.py"), "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("JobScout", help_result)
        self.assertFalse(any(phrase in help_result.casefold() for phrase in legacy_phrases))

    def test_public_release_metadata_and_durable_guidance_are_complete(self):
        license_path = ROOT / "LICENSE"
        self.assertTrue(license_path.is_file())
        license_text = license_path.read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 WilliamOrtizDev", license_text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", license_text)

        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        hermes_upstream_commit = "3a3dd5c5140d76f65a829150b1dece7e72b6552c"
        hermes_mit_notice = """Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the \"Software\"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""
        for required in (
            "Hermes Agent",
            "ats-scrapers",
            "OpenRoles",
            "CC BY-SA 4.0",
            "DuckDB",
            "Tectonic",
            "Playwright",
            "Photon",
            "patches/hermes-agent-local-fixes.patch",
            "Copyright (c) 2025 Nous Research",
            "not redistributed",
        ):
            self.assertIn(required, notices)
        self.assertIn(f"`{hermes_upstream_commit}`", notices)
        hermes_notice_section = notices.split(
            "### Hermes Agent MIT notice\n\n", 1
        )[1].split("\n\n## Discovery sources", 1)[0]
        self.assertEqual(hermes_notice_section, hermes_mit_notice)

        hermes_patch = (ROOT / "patches/hermes-agent-local-fixes.patch").read_text()
        patch_preimages = re.findall(
            r"^index ([0-9a-f]{10})\.\.[0-9a-f]{10}", hermes_patch, re.MULTILINE
        )
        self.assertEqual(
            patch_preimages,
            [
                "f4129cad43",
                "15ca2ea198",
                "589d4e2775",
                "1d629ae1df",
                "8c1d5b66c7",
                "11a7bf40c2",
            ],
        )

        self.assertFalse((ROOT / "docs/PUBLISHING.md").exists())
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn("docs/PUBLISHING.md", readme)
        self.assertIn("LICENSE", readme)
        self.assertIn("THIRD_PARTY_NOTICES.md", readme)

        security = (ROOT / "SECURITY.md").read_text()
        for required in (
            "Public-release gate",
            "Git history",
            "browser state",
            "backup archives",
            "application packets",
            "immutable version tag",
            "supported platforms and manual steps",
        ):
            self.assertIn(required, security)

    def test_retired_benchmark_vendor_is_absent_from_tracked_tree(self):
        forbidden = "fanta" + "stic"
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode().split("\0")
        findings = []
        for relative in filter(None, tracked):
            if forbidden in relative.casefold():
                findings.append(relative)
                continue
            path = ROOT / relative
            if path.is_file():
                try:
                    content = path.read_text()
                except UnicodeDecodeError:
                    continue
                if forbidden in content.casefold():
                    findings.append(relative)
        self.assertEqual(findings, [])

    def test_retired_scaffolding_is_not_tracked(self):
        tracked = set(
            subprocess.run(
                ["git", "ls-files"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        )
        retired = {
            "config/model-routing.example.yaml",
            "docs/plans/2026-09-07-jobscout-public-release.md",
            "state.example.json",
            "systemd/hermes-gateway.service.d/runtime-env.conf",
        }
        self.assertEqual(set(), retired & tracked)

    def test_private_source_registry_cannot_be_tracked(self):
        validator = load_validator()

        self.assertIn("config/sources.json", validator.private_paths)

    def test_candidate_runtime_artifacts_are_ignored_and_untracked(self):
        private_examples = (
            "applications/JOB-123/job.json",
            "browser-profiles/portal/Cookies",
            "release-evidence/dry-run.json",
            "backups/job-scout.tar.gz",
            "job-scout-backup.tgz",
            "job-scout-backup.ZIP",
            "job-scout.BACKUP",
            "candidate-resume.PDF",
            "candidate-profile.DOCX",
            "candidate-profile.docm",
            "candidate-template.dotx",
            "state.SQLITE",
            "state.sqlite-wal",
            "state.SQLite3",
            "state.sqlite3-shm",
        )
        for relative in private_examples:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.excludesFile=/dev/null",
                    "check-ignore",
                    "--quiet",
                    "--no-index",
                    relative,
                ],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(0, result.returncode, relative)

        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        private_prefixes = (
            "applications/",
            "backups/",
            "browser-profiles/",
            "release-evidence/",
            "resume/",
            "state/",
        )
        private_suffixes = (
            ".backup",
            ".bak",
            ".db",
            ".db-journal",
            ".db-shm",
            ".db-wal",
            ".doc",
            ".docm",
            ".docx",
            ".dotx",
            ".pdf",
            ".sqlite",
            ".sqlite-journal",
            ".sqlite-shm",
            ".sqlite-wal",
            ".sqlite3",
            ".sqlite3-journal",
            ".sqlite3-shm",
            ".sqlite3-wal",
            ".tar.gz",
            ".tgz",
            ".zip",
        )
        findings = [
            relative
            for relative in tracked
            if relative.casefold().startswith(private_prefixes)
            or relative.casefold().endswith(private_suffixes)
        ]
        self.assertEqual([], findings)

    def test_live_runtime_has_no_json_state_dependency(self):
        live_input = (ROOT / "scripts/live_scout_input.py").read_text()
        prompt = (ROOT / "cron/prompt.example.md").read_text()
        install = (ROOT / "scripts/install.sh").read_text()

        collect = (ROOT / "scripts/collect.py").read_text()
        pipeline = (ROOT / "job_scout/pipeline.py").read_text()

        self.assertNotIn("state.json", live_input)
        self.assertNotIn("lead-delivery-cursor.json", live_input)
        self.assertNotIn("state.json", prompt)
        self.assertIn("scripts/install_state.py", install)
        self.assertEqual(install.count("state.json"), 1)
        self.assertNotIn('"--state"', collect)
        self.assertNotIn("load_known_records", pipeline)
        self.assertNotIn("load_known_urls", pipeline)

    def test_live_collector_reads_the_private_workspace_profile(self):
        live_input = (ROOT / "scripts/live_scout_input.py").read_text()

        self.assertIn('PROFILE = JOB_SEARCH / "search-profile.json"', live_input)
        self.assertIn('"--profile",', live_input)
        self.assertIn('str(PROFILE),', live_input)
        self.assertIn("timeout=300", live_input)


    def test_openrouter_key_assignment_is_recognized_as_secret(self):
        validator = load_validator()
        pattern = validator.patterns["generic API token assignment"]

        self.assertRegex("OPENROUTER_API_KEY=secret-value", re.compile(pattern))

    def test_installer_is_portable_and_preserves_private_configuration(self):
        install = (ROOT / "scripts/install.sh").read_text()
        create_cron = (ROOT / "scripts/create-cron.sh").read_text()

        self.assertIn("copy_if_missing", install)
        self.assertIn("scripts/render_runtime.py", install)
        self.assertIn("APPLY_HERMES_PATCH", install)
        self.assertNotIn('install -m 700 "$ROOT/scripts/live_scout_input.py"', install)
        renderer = (ROOT / "scripts/render_runtime.py").read_text()
        self.assertIn("runpy.run_path", renderer)
        self.assertIn("$JOB_SEARCH_WORKDIR/cron-prompt.md", create_cron)
        self.assertNotIn("$ROOT/cron/prompt.md", create_cron)
        self.assertIn("HERMES_PROFILE", create_cron)

        migration = (ROOT / "docs/MIGRATION.md").read_text()
        self.assertIn("hermes profile export job-scout", migration)
        self.assertNotIn("hermes profile export default", migration)
        self.assertIn(
            'export JOB_SEARCH_HERMES_HOME="$HOME/.hermes/profiles/job-scout-migrated"',
            migration,
        )
        self.assertIn('"$JOB_SEARCH_HERMES_HOME/scripts/job-scout-shadow.py"', migration)
        self.assertNotIn('"$HERMES_HOME/scripts/job-scout-shadow.py"', migration)
        self.assertIn("./scripts/create-cron.sh", migration)
        self.assertIn("hermes cron remove <IMPORTED-JOB-ID>", migration)
        self.assertNotIn("hermes cron resume <JOB-ID>", migration)
        self.assertIn(
            'HERMES_HOME="$HOME/.hermes/profiles/job-scout" hermes cron pause <JOB-ID>',
            migration,
        )

    def test_create_cron_targets_profile_home_without_global_profile_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            log = root / "hermes.log"
            fake_hermes = binary / "hermes"
            fake_hermes.write_text(
                "#!/bin/sh\n"
                "printf '%s|%s\\n' \"$HERMES_HOME\" \"$*\" >> \"$HERMES_LOG\"\n"
            )
            fake_hermes.chmod(0o700)
            prompt = root / "prompt.md"
            prompt.write_text("safe prompt")
            profile_home = root / "hermes" / "profiles" / "job-scout"
            environment = {
                **os.environ,
                "PATH": f"{binary}:{os.environ['PATH']}",
                "HERMES_LOG": str(log),
                "HERMES_PROFILE": "job-scout",
                "JOB_SEARCH_HERMES_HOME": str(profile_home),
                "JOB_SEARCH_PROMPT": str(prompt),
                "JOB_SEARCH_WORKDIR": str(root / "jobs"),
                "PHOTON_TARGET": "photon:test",
            }

            subprocess.run(
                ["bash", str(ROOT / "scripts/create-cron.sh")],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(call.startswith(f"{profile_home}|") for call in calls))
            self.assertTrue(all("--profile" not in call for call in calls))
            self.assertIn("cron list --all", calls[0])
            self.assertIn(f"--workdir {root / 'jobs'}", calls[1])

    def test_bootstrap_documents_official_hermes_installer_and_doctor(self):
        bootstrap = (ROOT / "scripts/bootstrap.sh").read_text()

        self.assertIn("https://hermes-agent.nousresearch.com/install.sh", bootstrap)
        self.assertIn("scripts/install_tectonic.py", bootstrap)
        self.assertIn("scripts/portable.py", bootstrap)
        self.assertIn("doctor || true", bootstrap)
        self.assertIn("--dependencies-only", bootstrap)
