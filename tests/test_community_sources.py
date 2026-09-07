from datetime import datetime, timezone
import unittest

from job_scout.community_sources import (
    collect_community_sources,
    merge_collection_results,
    select_openroles_chunks,
)
from job_scout.collector import CollectionResult
from job_scout.models import Candidate


class StubCommunityClient:
    def __init__(self, ats_rows=None, openroles_rows=None, errors=None):
        self.ats_rows = ats_rows or []
        self.openroles_rows = openroles_rows or []
        self.errors = errors or {}
        self.calls = []

    def fetch_ats_scrapers(self, entry, *, cutoff, titles):
        self.calls.append(("ats_scrapers", entry, cutoff, tuple(titles)))
        if "ats_scrapers" in self.errors:
            raise self.errors["ats_scrapers"]
        return self.ats_rows

    def fetch_openroles(self, entry, *, cutoff, titles):
        self.calls.append(("openroles", entry, cutoff, tuple(titles)))
        if "openroles" in self.errors:
            raise self.errors["openroles"]
        return self.openroles_rows


class CommunitySourceTests(unittest.TestCase):
    def test_normalizes_machine_readable_employment_type(self):
        client = StubCommunityClient(
            ats_rows=[
                {
                    "url": "https://jobs.ashbyhq.com/acme/1",
                    "title": "DevOps Engineer",
                    "company": "Acme",
                    "ats_type": "ashby",
                    "ats_id": "1",
                    "employment_type": "FULL_TIME",
                }
            ]
        )

        result = collect_community_sources(
            {"ats_scrapers": self.config["ats_scrapers"]},
            client,
            titles=self.titles,
            posted_within_days=14,
            now=self.now,
        )

        self.assertEqual(result.jobs[0].employment_type, "FULL TIME")

    def test_merge_preserves_direct_source_when_community_url_duplicates_it(self):
        direct = Candidate(
            source="greenhouse",
            tenant="acme",
            source_id="1",
            company="Acme",
            title="Platform Engineer",
            location="Remote",
            description="Contract Linux",
            employment_type="Contract",
            remote=True,
            posted_at=None,
            canonical_url="https://job-boards.greenhouse.io/acme/jobs/1",
            apply_url="https://job-boards.greenhouse.io/acme/jobs/1/apply",
            raw={},
        )
        community = Candidate(
            **{
                **direct.__dict__,
                "source": "openroles",
                "tenant": "greenhouse:acme",
                "apply_url": direct.canonical_url + "/",
            }
        )

        merged = merge_collection_results(
            CollectionResult([direct], [], 1, 1),
            CollectionResult([community], [], 2, 2),
        )

        self.assertEqual(merged.jobs, [direct])
        self.assertEqual(merged.configured_sources, 3)
        self.assertEqual(merged.successful_sources, 3)

    def test_merge_deduplicates_by_source_identity_when_urls_change(self):
        first = Candidate(
            source="openroles", tenant="workday:acme", source_id="same",
            company="Acme", title="Platform Engineer", location="Remote",
            description="", employment_type=None, remote=True, posted_at=None,
            canonical_url="https://jobs.example.com/old", apply_url="https://jobs.example.com/old",
            raw={},
        )
        moved = Candidate(
            **{**first.__dict__, "canonical_url": "https://jobs.example.com/new", "apply_url": "https://jobs.example.com/new"}
        )

        merged = merge_collection_results(
            CollectionResult([first], [], 1, 1), CollectionResult([moved], [], 1, 1)
        )

        self.assertEqual(merged.jobs, [first])

    def test_merge_preserves_same_source_id_for_different_companies(self):
        first = Candidate(
            source="ats_scrapers", tenant="workday", source_id="same",
            company="Alpha", title="Platform Engineer", location="Remote",
            description="", employment_type=None, remote=True, posted_at=None,
            canonical_url="https://jobs.example.com/alpha", apply_url="https://jobs.example.com/alpha",
            raw={},
        )
        second = Candidate(
            **{
                **first.__dict__,
                "company": "Beta",
                "canonical_url": "https://jobs.example.com/beta",
                "apply_url": "https://jobs.example.com/beta",
            }
        )

        merged = merge_collection_results(
            CollectionResult([first], [], 1, 1), CollectionResult([second], [], 1, 1)
        )

        self.assertEqual(merged.jobs, [first, second])

    def setUp(self):
        self.config = {
            "ats_scrapers": [
                {
                    "name": "hosted-dataset",
                    "manifest_url": "https://storage.stapply.ai/jobhive/v1/manifest.json",
                }
            ],
            "openroles": [
                {
                    "name": "public-slim-index",
                    "manifest_url": "https://openroles.today/data/manifest.json",
                }
            ],
        }
        self.titles = ["DevOps Engineer", "Platform Engineer"]
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def test_collects_and_normalizes_both_upstreams(self):
        client = StubCommunityClient(
            ats_rows=[
                {
                    "url": "https://jobs.ashbyhq.com/acme/1",
                    "apply_url": "https://jobs.ashbyhq.com/acme/1/apply",
                    "title": "Senior DevOps Engineer",
                    "company": "Acme",
                    "ats_type": "workday",
                    "ats_id": "1",
                    "location": "United States - Remote",
                    "is_remote": "true",
                    "employment_type": "Contract",
                    "description": "Linux and Terraform contract",
                    "posted_at": "2026-09-05T00:00:00Z",
                }
            ],
            openroles_rows=[
                {
                    "i": "abc123",
                    "a": "icims",
                    "t": "example",
                    "ti": "Platform Engineer",
                    "c": "Example",
                    "w": "remote",
                    "loc": "United States",
                    "cc": "US",
                    "p": "2026-09-04T00:00:00Z",
                    "cm": 70,
                    "cmax": 90,
                    "cur": "USD",
                    "u": "https://job-boards.greenhouse.io/example/jobs/abc123",
                    "s": 0,
                }
            ],
        )

        result = collect_community_sources(
            self.config,
            client,
            titles=self.titles,
            posted_within_days=14,
            now=self.now,
        )

        self.assertEqual(result.configured_sources, 2)
        self.assertEqual(result.successful_sources, 2)
        self.assertEqual(result.failures, [])
        self.assertEqual([job.source for job in result.jobs], ["ats_scrapers", "openroles"])
        self.assertEqual(result.jobs[0].tenant, "workday")
        self.assertEqual(result.jobs[0].employment_type, "Contract")
        self.assertTrue(result.jobs[0].remote)
        self.assertEqual(result.jobs[1].tenant, "icims:example")
        self.assertTrue(result.jobs[1].remote)
        self.assertEqual(result.jobs[1].raw["compensation_min"], 70)

    def test_deduplicates_cross_project_rows_by_application_url(self):
        shared = "https://jobs.ashbyhq.com/acme/1"
        client = StubCommunityClient(
            ats_rows=[
                {
                    "url": shared,
                    "apply_url": shared,
                    "title": "DevOps Engineer",
                    "company": "Acme",
                    "ats_type": "greenhouse",
                    "ats_id": "1",
                    "location": "Remote",
                    "is_remote": True,
                    "description": "Contract",
                }
            ],
            openroles_rows=[
                {
                    "i": "duplicate",
                    "a": "greenhouse",
                    "t": "acme",
                    "ti": "DevOps Engineer",
                    "c": "Acme",
                    "w": "remote",
                    "loc": "Remote",
                    "u": shared + "/",
                    "s": 0,
                }
            ],
        )

        result = collect_community_sources(
            self.config,
            client,
            titles=self.titles,
            posted_within_days=14,
            now=self.now,
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].source, "ats_scrapers")

    def test_collector_preserves_same_ats_id_for_different_companies(self):
        def row(company, slug):
            return {
                "url": f"https://jobs.ashbyhq.com/{slug}/same",
                "title": "DevOps Engineer",
                "company": company,
                "ats_type": "workday",
                "ats_id": "same",
                "location": "Remote",
                "is_remote": True,
                "description": "Contract",
            }

        result = collect_community_sources(
            self.config,
            StubCommunityClient(ats_rows=[row("Alpha", "alpha"), row("Beta", "beta")]),
            titles=self.titles,
            posted_within_days=14,
            now=self.now,
        )

        self.assertEqual([job.company for job in result.jobs], ["Alpha", "Beta"])

    def test_upstream_failure_is_nonfatal_to_the_other_project(self):
        client = StubCommunityClient(
            openroles_rows=[], errors={"ats_scrapers": RuntimeError("dataset unavailable")}
        )

        result = collect_community_sources(
            self.config,
            client,
            titles=self.titles,
            posted_within_days=14,
            now=self.now,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].source, "ats_scrapers")
        self.assertIn("dataset unavailable", result.failures[0].error)

    def test_rejects_malformed_rows_without_losing_valid_rows(self):
        client = StubCommunityClient(
            ats_rows=[
                {"title": "missing identifiers"},
                {"title": "also missing identifiers"},
            ],
            openroles_rows=[
                {
                    "i": "valid",
                    "a": "workable",
                    "t": "acme",
                    "ti": "Platform Engineer",
                    "c": "Acme",
                    "w": "remote",
                    "loc": "Remote",
                    "u": "https://apply.workable.com/acme/j/valid",
                    "s": 0,
                }
            ],
        )

        result = collect_community_sources(
            self.config,
            client,
            titles=self.titles,
            posted_within_days=14,
            now=self.now,
        )

        self.assertEqual([job.source_id for job in result.jobs], ["valid"])
        normalization_failures = [
            failure
            for failure in result.failures
            if "normalization failed" in failure.error
        ]
        self.assertEqual(len(normalization_failures), 1)
        self.assertIn("2 rows", normalization_failures[0].error)

    def test_selects_only_recent_bounded_openroles_chunks(self):
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [
                {
                    "file": "slim/slim-new.json.gz",
                    "sha": "a" * 16,
                    "rows": 20_000,
                    "bytes_gz": 1_000,
                    "bytes_raw": 8_000,
                    "posted_min": "2026-09-04T00:00:00Z",
                    "posted_max": "2026-09-06T00:00:00Z",
                    "has_null_posted": False,
                },
                {
                    "file": "slim/slim-old.json.gz",
                    "sha": "b" * 16,
                    "rows": 20_000,
                    "bytes_gz": 1_000,
                    "bytes_raw": 8_000,
                    "posted_min": "2026-01-01T00:00:00Z",
                    "posted_max": "2026-01-02T00:00:00Z",
                    "has_null_posted": False,
                },
            ],
        }

        selected = select_openroles_chunks(
            manifest,
            cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
            max_chunks=16,
            max_compressed_bytes=32 * 1024 * 1024,
        )

        self.assertEqual([chunk["file"] for chunk in selected], ["slim/slim-new.json.gz"])

    def test_rejects_openroles_manifest_that_exceeds_chunk_budget(self):
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [
                {
                    "file": "slim/slim-huge.json.gz",
                    "sha": "a" * 16,
                    "rows": 20_000,
                    "bytes_gz": 40 * 1024 * 1024,
                    "bytes_raw": 12 * 1024 * 1024,
                    "posted_min": "2026-09-04T00:00:00Z",
                    "posted_max": "2026-09-06T00:00:00Z",
                    "has_null_posted": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "compressed byte budget"):
            select_openroles_chunks(
                manifest,
                cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                max_chunks=16,
                max_compressed_bytes=32 * 1024 * 1024,
            )

    def test_rejects_openroles_manifest_that_exceeds_raw_byte_budget(self):
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [
                {
                    "file": "slim/slim-new.json.gz",
                    "sha": "a" * 16,
                    "rows": 20_000,
                    "bytes_gz": 1_000,
                    "bytes_raw": 8_000,
                    "posted_max": "2026-09-06T00:00:00Z",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "raw byte budget"):
            select_openroles_chunks(
                manifest,
                cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                max_chunks=16,
                max_compressed_bytes=32 * 1024 * 1024,
                max_raw_bytes=7_999,
                max_candidate_rows=20_000,
            )

    def test_rejects_openroles_manifest_that_exceeds_candidate_row_budget(self):
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [
                {
                    "file": "slim/slim-new.json.gz",
                    "sha": "a" * 16,
                    "rows": 20_000,
                    "bytes_gz": 1_000,
                    "bytes_raw": 8_000,
                    "posted_max": "2026-09-06T00:00:00Z",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "candidate row budget"):
            select_openroles_chunks(
                manifest,
                cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                max_chunks=16,
                max_compressed_bytes=32 * 1024 * 1024,
                max_raw_bytes=16_000,
                max_candidate_rows=19_999,
            )


if __name__ == "__main__":
    unittest.main()
