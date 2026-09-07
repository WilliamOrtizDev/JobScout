import json
import tempfile
import unittest
from pathlib import Path

from job_scout.http import JsonResult
from job_scout.collector import CollectionResult
from job_scout.models import Candidate
from job_scout.pipeline import (
    PipelineResult,
    run_collected_pipeline,
    run_pipeline,
    write_result,
)
from job_scout.sources import normalize_greenhouse


class PipelineOutputTests(unittest.TestCase):
    def test_filters_an_already_collected_community_batch(self):
        job = Candidate(
            source="openroles",
            tenant="workday:acme",
            source_id="1",
            company="Acme",
            title="Platform Engineer",
            location="Remote - United States",
            description="C2C Linux infrastructure",
            employment_type="Contract",
            remote=True,
            posted_at="2026-09-05T00:00:00Z",
            canonical_url="https://jobs.example.com/jobs/1",
            apply_url="https://jobs.example.com/jobs/1/apply",
            raw={},
        )
        collected = CollectionResult(
            jobs=[job], failures=[], configured_sources=2, successful_sources=2
        )

        result = run_collected_pipeline(
            collected,
            {
                "targets": {
                    "titles": ["Platform Engineer"],
                    "remote_only": True,
                    "contract_only": True,
                }
            },
            known_records={},
        )

        self.assertEqual(result.jobs, [job])
        self.assertEqual(result.configured_sources, 2)
        self.assertEqual(result.successful_sources, 2)

    def test_suppresses_candidate_when_application_url_is_already_known(self):
        hosted_url = "https://jobs.lever.co/acme/role-1"
        apply_url = "https://jobs.lever.co/acme/role-1/apply"

        class LeverClient:
            def get(self, requested_url, *, headers=None):
                return JsonResult(
                    data=[
                        {
                            "id": "role-1",
                            "text": "Platform Engineer",
                            "categories": {
                                "location": "Remote",
                                "commitment": "Contract",
                            },
                            "workplaceType": "remote",
                            "descriptionPlain": "Linux infrastructure",
                            "hostedUrl": hosted_url,
                            "applyUrl": apply_url,
                        }
                    ],
                    status=200,
                    from_cache=False,
                )

        result = run_pipeline(
            {"lever": [{"tenant": "acme", "company": "Acme"}]},
            {
                "targets": {
                    "titles": ["Platform Engineer"],
                    "remote_only": True,
                    "contract_only": True,
                }
            },
            LeverClient(),
            known_records={apply_url: None},
        )

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.rejected_counts, {"seen": 1})


    def test_preserves_all_hashes_for_colliding_normalized_state_urls(self):
        url = "https://boards.greenhouse.io/acme/jobs/9"
        raw = {
            "id": 9,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "C2C Linux contract",
            "absolute_url": url,
        }
        current_hash = normalize_greenhouse(
            raw, tenant="acme", company="Acme"
        ).to_dict()["content_hash"]

        class OneJobClient:
            def get(self, requested_url, *, headers=None):
                data = raw if requested_url.endswith("/jobs/9") else {"jobs": [raw]}
                return JsonResult(data=data, status=200, from_cache=False)

        result = run_pipeline(
            {"greenhouse": [{"tenant": "acme", "company": "Acme"}]},
            {"targets": {"titles": ["Platform Engineer"], "contract_only": True}},
            OneJobClient(),
            known_records={url: current_hash, url + "?utm_source=other": "other-hash"},
        )

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.rejected_counts, {"seen": 1})

    def test_reemits_seen_url_when_content_hash_changes(self):
        url = "https://boards.greenhouse.io/acme/jobs/7"
        original_raw = {
            "id": 7,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "C2C Linux contract",
            "absolute_url": url,
        }
        known_hash = normalize_greenhouse(
            original_raw, tenant="acme", company="Acme"
        ).to_dict()["content_hash"]

        class ChangedJobClient:
            def get(self, requested_url, *, headers=None):
                raw = {**original_raw, "content": "C2C Linux and Kubernetes contract"}
                data = raw if requested_url.endswith("/jobs/7") else {"jobs": [raw]}
                return JsonResult(data=data, status=200, from_cache=False)

        result = run_pipeline(
            {"greenhouse": [{"tenant": "acme", "company": "Acme"}]},
            {
                "targets": {
                    "titles": ["Platform Engineer"],
                    "remote_only": True,
                    "contract_only": True,
                }
            },
            ChangedJobClient(),
            known_records={url: known_hash},
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.rejected_counts, {})

    def test_excludes_urls_already_present_in_durable_state(self):
        url = "https://boards.greenhouse.io/acme/jobs/1?utm_source=feed"

        class OneJobClient:
            def get(self, requested_url, *, headers=None):
                raw = {
                    "id": 1,
                    "title": "Platform Engineer",
                    "location": {"name": "Remote"},
                    "content": "C2C contract Linux",
                    "absolute_url": url,
                }
                data = raw if requested_url.endswith("/jobs/1") else {"jobs": [raw]}
                return JsonResult(
                    data=data,
                    status=200,
                    from_cache=False,
                )

        result = run_pipeline(
            {"greenhouse": [{"tenant": "acme", "company": "Acme"}]},
            {
                "targets": {
                    "titles": ["Platform Engineer"],
                    "remote_only": True,
                    "contract_only": True,
                }
            },
            OneJobClient(),
            known_urls={"https://boards.greenhouse.io/acme/jobs/1"},
        )

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.rejected_counts, {"seen": 1})

    def test_writes_model_ready_snapshot_atomically_without_raw_payloads(self):
        job = Candidate(
            source="greenhouse",
            tenant="acme",
            source_id="1",
            company="Acme",
            title="Platform Engineer",
            location="Remote",
            description="Linux contract",
            employment_type="Contract",
            remote=True,
            posted_at=None,
            canonical_url="https://example.test/jobs/1",
            apply_url="https://example.test/jobs/1/apply",
            raw={"large": "upstream payload"},
        )
        result = PipelineResult(
            jobs=[job],
            source_failures=[],
            rejected_counts={"title": 2},
            configured_sources=2,
            successful_sources=1,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "candidates.json"
            write_result(result, output)
            saved = json.loads(output.read_text())

        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["counts"]["accepted"], 1)
        self.assertEqual(saved["counts"]["configured_sources"], 2)
        self.assertEqual(saved["counts"]["successful_sources"], 1)
        self.assertEqual(saved["counts"]["rejected"], {"title": 2})
        self.assertNotIn("raw", saved["jobs"][0])
        self.assertEqual(saved["jobs"][0]["apply_url"], job.apply_url)


if __name__ == "__main__":
    unittest.main()
