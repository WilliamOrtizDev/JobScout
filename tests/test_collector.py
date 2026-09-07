import threading
import unittest

from job_scout.collector import canonical_url_key, collect_sources
from job_scout.http import JsonResult
from scripts.collect import configured_source_count


class StubClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.urls = []

    def get(self, url, *, headers=None):
        self.urls.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return JsonResult(data=outcome, from_cache=False, status=200)


class CollectorTests(unittest.TestCase):
    def test_canonical_url_key_normalizes_default_port_and_trailing_dot(self):
        expected = "https://jobs.example.com/posting"

        self.assertEqual(
            canonical_url_key("https://jobs.example.com.:443/posting/"), expected
        )

    def test_rejects_excessive_worker_count(self):
        with self.assertRaisesRegex(ValueError, "max_workers"):
            collect_sources({}, StubClient({}), max_workers=33)

    def test_cli_counts_malformed_source_collection_as_failed_source(self):
        configured = configured_source_count(
            {
                "greenhouse": None,
                "lever": [{"tenant": "good", "company": "Good"}],
            }
        )

        self.assertEqual(configured, 2)

    def test_cli_rejects_non_object_source_configuration(self):
        with self.assertRaisesRegex(ValueError, "must be an object"):
            configured_source_count([])

    def test_rejects_non_list_feed_content_without_aborting_other_sources(self):
        smart_url = (
            "https://api.smartrecruiters.com/v1/companies/bad/postings?limit=100"
        )
        lever_url = "https://api.lever.co/v0/postings/good?mode=json&limit=100&skip=0"
        client = StubClient(
            {
                smart_url: {"totalFound": 1, "content": "not-a-list"},
                lever_url: [],
            }
        )

        result = collect_sources(
            {
                "smartrecruiters": [{"tenant": "bad", "company": "Bad"}],
                "lever": [{"tenant": "good", "company": "Good"}],
            },
            client,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("feed content must be a list", result.failures[0].error)

    def test_malformed_job_row_does_not_discard_valid_rows(self):
        url = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        client = StubClient(
            {
                url: [
                    "not-an-object",
                    {
                        "id": "valid",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/valid",
                    },
                ]
            }
        )

        result = collect_sources(
            {"lever": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual([job.source_id for job in result.jobs], ["valid"])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("normalization failed", result.failures[0].error)

    def test_paginates_large_lever_feeds(self):
        first = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        second = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=100"

        def row(identifier):
            return {
                "id": identifier,
                "text": "Platform Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{identifier}",
            }

        client = StubClient(
            {
                first: [row(str(index)) for index in range(100)],
                second: [row("100")],
            }
        )

        result = collect_sources(
            {"lever": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.jobs), 101)
        self.assertEqual(client.urls, [first, second])

    def test_lever_pagination_continues_from_the_most_recent_page_size(self):
        first = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        second = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=100"
        third = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=200"

        def row(identifier):
            return {
                "id": str(identifier),
                "text": "Platform Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{identifier}",
            }

        cases = (
            (
                "exactly one full page",
                {first: [row(index) for index in range(100)], second: []},
                100,
                [first, second],
            ),
            (
                "more than two full pages",
                {
                    first: [row(index) for index in range(100)],
                    second: [row(index) for index in range(100, 200)],
                    third: [row(200)],
                },
                201,
                [first, second, third],
            ),
        )
        for label, outcomes, expected_count, expected_urls in cases:
            with self.subTest(label=label):
                client = StubClient(outcomes)
                result = collect_sources(
                    {"lever": [{"tenant": "acme", "company": "Acme"}]}, client
                )

                self.assertEqual(result.successful_sources, 1)
                self.assertEqual(len(result.jobs), expected_count)
                self.assertEqual(client.urls, expected_urls)

    def test_rejects_overlapping_lever_pages(self):
        first = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        second = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=100"

        def row(identifier):
            return {
                "id": identifier,
                "text": "Platform Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{identifier}",
            }

        client = StubClient(
            {
                first: [row(str(index)) for index in range(100)],
                second: [row("0")],
            }
        )

        result = collect_sources(
            {"lever": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertTrue(any("duplicate posting" in failure.error for failure in result.failures))
        self.assertEqual(client.urls, [first, second])

    def test_rejects_lever_page_larger_than_requested_limit(self):
        url = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        rows = [
            {
                "id": str(index),
                "text": "Platform Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{index}",
            }
            for index in range(101)
        ]

        result = collect_sources(
            {"lever": [{"tenant": "acme", "company": "Acme"}]},
            StubClient({url: rows}),
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertEqual(result.jobs, [])
        self.assertTrue(any("page exceeds" in failure.error for failure in result.failures))

    def test_rejects_lever_followup_page_larger_than_requested_limit(self):
        first = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        second = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=100"

        def row(identifier):
            return {
                "id": identifier,
                "text": "Platform Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{identifier}",
            }

        client = StubClient(
            {
                first: [row(str(index)) for index in range(100)],
                second: [row(str(index)) for index in range(100, 201)],
            }
        )

        result = collect_sources(
            {"lever": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertEqual(result.jobs, [])
        self.assertTrue(any("page exceeds" in failure.error for failure in result.failures))

    def test_reports_malformed_source_collection_without_aborting_valid_sources(self):
        client = StubClient(
            {"https://api.lever.co/v0/postings/good?mode=json&limit=100&skip=0": []}
        )

        result = collect_sources(
            {
                "greenhouse": None,
                "lever": [{"tenant": "good", "company": "Good"}],
            },
            client,
        )

        self.assertEqual(result.configured_sources, 2)
        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("source entries must be a list", result.failures[0].error)

    def test_reports_malformed_source_entry_without_aborting_other_sources(self):
        client = StubClient(
            {
                "https://api.lever.co/v0/postings/good?mode=json&limit=100&skip=0": [],
            }
        )

        result = collect_sources(
            {
                "greenhouse": ["not-an-object"],
                "lever": [{"tenant": "good", "company": "Good"}],
            },
            client,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("source entry must be an object", result.failures[0].error)

    def test_rejects_tenant_values_that_could_escape_the_expected_api_path(self):
        client = StubClient({})

        result = collect_sources(
            {
                "smartrecruiters": [
                    {"tenant": "../../metadata", "company": "Untrusted"}
                ]
            },
            client,
        )

        self.assertEqual(result.jobs, [])
        self.assertEqual(client.urls, [])
        self.assertIn("invalid tenant", result.failures[0].error)

    def test_malformed_detail_payload_does_not_abort_other_sources(self):
        greenhouse_list = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        greenhouse_detail = greenhouse_list + "/1"
        lever_list = "https://api.lever.co/v0/postings/good?mode=json&limit=100&skip=0"
        client = StubClient(
            {
                greenhouse_list: {
                    "jobs": [{"id": 1, "title": "Platform Engineer"}]
                },
                greenhouse_detail: ["not", "an", "object"],
                lever_list: [
                    {
                        "id": "lever-1",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/good/lever-1",
                    }
                ],
            }
        )

        result = collect_sources(
            {
                "greenhouse": [{"tenant": "acme", "company": "Acme"}],
                "lever": [{"tenant": "good", "company": "Good"}],
            },
            client,
            title_filter=lambda _title: True,
        )

        self.assertEqual([job.source_id for job in result.jobs], ["lever-1"])
        self.assertTrue(any("detail must be an object" in f.error for f in result.failures))

    def test_preserves_other_greenhouse_jobs_when_one_detail_fails(self):
        list_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        client = StubClient(
            {
                list_url: {
                    "jobs": [
                        {"id": 1, "title": "Platform Engineer"},
                        {"id": 2, "title": "Platform Engineer"},
                    ]
                },
                f"{list_url}/1": {
                    "id": 1,
                    "title": "Platform Engineer",
                    "location": {"name": "Remote"},
                    "content": "C2C Linux contract",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                },
                f"{list_url}/2": RuntimeError("detail unavailable"),
            }
        )

        result = collect_sources(
            {"greenhouse": [{"tenant": "acme", "company": "Acme"}]},
            client,
            title_filter=lambda _title: True,
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("job 2", result.failures[0].error)

    def test_greenhouse_fetches_details_only_for_matching_titles(self):
        list_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        target_detail = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"
        unrelated_detail = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/2"
        client = StubClient(
            {
                list_url: {
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Platform Engineer",
                            "location": {"name": "Remote"},
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        },
                        {
                            "id": 2,
                            "title": "Account Executive",
                            "location": {"name": "Remote"},
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                        },
                    ]
                },
                target_detail: {
                    "id": 1,
                    "title": "Platform Engineer",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "content": "C2C Linux contract",
                },
            }
        )

        result = collect_sources(
            {"greenhouse": [{"tenant": "acme", "company": "Acme"}]},
            client,
            title_filter=lambda title: "platform" in title.lower(),
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertIn(target_detail, client.urls)
        self.assertNotIn(unrelated_detail, client.urls)

    def test_keeps_partial_results_when_one_tenant_fails(self):
        greenhouse_url = (
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
        )
        lever_url = "https://api.lever.co/v0/postings/broken?mode=json&limit=100&skip=0"
        client = StubClient(
            {
                greenhouse_url: {
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Platform Engineer",
                            "location": {"name": "Remote"},
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                            "content": "Contract Linux",
                        }
                    ]
                },
                lever_url: RuntimeError("upstream unavailable"),
            }
        )
        config = {
            "greenhouse": [{"tenant": "acme", "company": "Acme"}],
            "lever": [{"tenant": "broken", "company": "Broken Co"}],
        }

        result = collect_sources(config, client)

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].source, "greenhouse")
        self.assertEqual(result.configured_sources, 2)
        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].source, "lever")
        self.assertEqual(result.failures[0].tenant, "broken")
        self.assertIn("upstream unavailable", result.failures[0].error)

    def test_malformed_smartrecruiters_pagination_metadata_does_not_abort_other_sources(self):
        smart_url = (
            "https://api.smartrecruiters.com/v1/companies/bad/postings?limit=100"
        )
        lever_url = "https://api.lever.co/v0/postings/good?mode=json&limit=100&skip=0"
        client = StubClient(
            {
                smart_url: {"totalFound": "not-a-number", "content": []},
                lever_url: [],
            }
        )

        result = collect_sources(
            {
                "smartrecruiters": [{"tenant": "bad", "company": "Bad"}],
                "lever": [{"tenant": "good", "company": "Good"}],
            },
            client,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("pagination metadata", result.failures[0].error)

    def test_rejects_non_integer_smartrecruiters_pagination_metadata(self):
        url = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        for field in ("totalFound", "limit", "offset"):
            for value in (True, 1.5, "1"):
                with self.subTest(field=field, value=value):
                    metadata = {"totalFound": 0, "limit": 100, "offset": 0, "content": []}
                    metadata[field] = value
                    result = collect_sources(
                        {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
                        StubClient({url: metadata}),
                    )
                    self.assertEqual(result.successful_sources, 0)
                    self.assertIn("must be an integer", result.failures[0].error)

    def test_requires_smartrecruiters_total_found(self):
        url = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        client = StubClient({url: {"limit": 100, "offset": 0, "content": []}})

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertIn("pagination metadata", result.failures[0].error)

    def test_rejects_nonzero_initial_smartrecruiters_offset(self):
        url = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        row = {
            "id": "offset-one",
            "name": "Platform Engineer",
            "location": {"fullLocation": "Remote", "remote": True},
            "applyUrl": "https://jobs.smartrecruiters.com/acme/offset-one",
        }
        client = StubClient(
            {url: {"totalFound": 1, "limit": 100, "offset": 1, "content": [row]}}
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertIn("pagination metadata", result.failures[0].error)

    def test_rejects_overlapping_smartrecruiters_pages(self):
        first = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        second = first + "&offset=1"
        row = {
            "id": "same",
            "name": "Platform Engineer",
            "location": {"fullLocation": "Remote", "remote": True},
            "applyUrl": "https://jobs.smartrecruiters.com/acme/same",
        }
        client = StubClient(
            {
                first: {"totalFound": 2, "limit": 1, "offset": 0, "content": [row]},
                second: {"totalFound": 2, "limit": 1, "offset": 1, "content": [row]},
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertTrue(any("duplicate posting" in f.error for f in result.failures))

    def test_advances_smartrecruiters_offset_by_rows_received(self):
        first = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        second = first + "&offset=1"
        def row(identifier):
            return {
                "id": identifier,
                "name": "Platform Engineer",
                "location": {"fullLocation": "Remote", "remote": True},
                "typeOfEmployment": {"label": "Contract"},
                "jobAd": {"sections": {}},
                "applyUrl": f"https://jobs.smartrecruiters.com/acme/{identifier}",
            }
        client = StubClient(
            {
                first: {"totalFound": 2, "limit": 100, "offset": 0, "content": [row("1")]},
                second: {"totalFound": 2, "limit": 100, "offset": 1, "content": [row("2")]},
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual([job.source_id for job in result.jobs], ["1", "2"])
        self.assertEqual(client.urls[:2], [first, second])

    def test_rejects_inconsistent_smartrecruiters_followup_metadata(self):
        first = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        second = first + "&offset=1"
        row = {
            "id": "1",
            "name": "Platform Engineer",
            "location": {"fullLocation": "Remote", "remote": True},
            "typeOfEmployment": {"label": "Contract"},
            "jobAd": {"sections": {}},
            "applyUrl": "https://jobs.smartrecruiters.com/acme/1",
        }
        client = StubClient(
            {
                first: {"totalFound": 2, "limit": 1, "offset": 0, "content": [row]},
                second: {
                    "totalFound": 2,
                    "limit": 1,
                    "offset": 0,
                    "content": [{**row, "id": "2", "applyUrl": "https://jobs.smartrecruiters.com/acme/2"}],
                },
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertIn("pagination metadata", result.failures[0].error)

    def test_marks_incomplete_smartrecruiters_pagination_failed(self):
        first = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        second = first + "&offset=1"
        client = StubClient(
            {
                first: {
                    "totalFound": 3,
                    "limit": 2,
                    "offset": 0,
                    "content": [{"id": "1", "name": "Account Executive"}],
                },
                second: {
                    "totalFound": 3,
                    "limit": 2,
                    "offset": 1,
                    "content": [{"id": "2", "name": "Account Executive"}],
                },
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
        )

        self.assertEqual(result.successful_sources, 0)
        self.assertTrue(
            any("incomplete pagination" in failure.error for failure in result.failures)
        )

    def test_rejects_smartrecruiters_followup_page_over_limit(self):
        first = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        second = first + "&offset=100"
        rows = [{"id": str(index), "name": "Platform Engineer"} for index in range(101)]
        first_rows = [
            {"id": f"first-{index}", "name": "Platform Engineer"}
            for index in range(100)
        ]
        client = StubClient(
            {
                first: {"totalFound": 201, "limit": 100, "offset": 0, "content": first_rows},
                second: {"totalFound": 201, "limit": 100, "offset": 100, "content": rows},
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
        )

        self.assertEqual(client.urls, [first, second])
        self.assertEqual(result.successful_sources, 0)
        self.assertIn("page exceeds", result.failures[0].error)

    def test_rejects_smartrecruiters_page_over_declared_limit(self):
        url = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        rows = [{"id": str(index), "name": "Platform Engineer"} for index in range(101)]
        client = StubClient(
            {url: {"totalFound": 101, "limit": 100, "offset": 0, "content": rows}}
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
        )

        self.assertEqual(client.urls, [url])
        self.assertEqual(result.successful_sources, 0)
        self.assertIn("page exceeds", result.failures[0].error)

    def test_rejects_excessive_smartrecruiters_pagination_without_followup_requests(self):
        url = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        client = StubClient(
            {url: {"totalFound": 1_000_000, "content": []}}
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
        )

        self.assertEqual(client.urls, [url])
        self.assertEqual(result.successful_sources, 0)
        self.assertIn("pagination metadata", result.failures[0].error)

    def test_paginates_smartrecruiters_postings(self):
        first_url = (
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        )
        second_url = first_url + "&offset=1"
        first_detail = "https://api.smartrecruiters.com/v1/companies/acme/postings/1"
        second_detail = "https://api.smartrecruiters.com/v1/companies/acme/postings/2"
        client = StubClient(
            {
                first_url: {
                    "totalFound": 2,
                    "limit": 1,
                    "offset": 0,
                    "content": [{"id": "1", "name": "Platform Engineer"}],
                },
                second_url: {
                    "totalFound": 2,
                    "limit": 1,
                    "offset": 1,
                    "content": [{"id": "2", "name": "SRE"}],
                },
                first_detail: {
                    "id": "1",
                    "name": "Platform Engineer",
                    "location": {"fullLocation": "Remote", "remote": True},
                    "typeOfEmployment": {"label": "Contract"},
                    "jobAd": {"sections": {}},
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/1",
                },
                second_detail: {
                    "id": "2",
                    "name": "SRE",
                    "location": {"fullLocation": "Remote", "remote": True},
                    "typeOfEmployment": {"label": "Contract"},
                    "jobAd": {"sections": {}},
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/2",
                },
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
            title_filter=lambda _title: True,
        )

        self.assertEqual([job.source_id for job in result.jobs], ["1", "2"])
        self.assertIn(second_url, client.urls)

    def test_filters_smartrecruiters_titles_before_detail_requests(self):
        list_url = (
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        )
        target_detail = "https://api.smartrecruiters.com/v1/companies/acme/postings/42"
        client = StubClient(
            {
                list_url: {
                    "totalFound": 2,
                    "limit": 100,
                    "offset": 0,
                    "content": [
                        {"id": "42", "name": "Platform Engineer", "ref": target_detail},
                        {
                            "id": "99",
                            "name": "Account Executive",
                            "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/99",
                        },
                    ]
                },
                target_detail: {
                    "id": "42",
                    "name": "Platform Engineer",
                    "location": {"fullLocation": "Remote"},
                    "jobAd": {"sections": {}},
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/42-platform",
                },
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
            title_filter=lambda title: "platform" in title.lower(),
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertNotIn(
            "https://api.smartrecruiters.com/v1/companies/acme/postings/99",
            client.urls,
        )

    def test_fetches_smartrecruiters_details_for_apply_url_and_description(self):
        list_url = (
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
        )
        detail_url = "https://api.smartrecruiters.com/v1/companies/acme/postings/42"
        client = StubClient(
            {
                list_url: {
                    "totalFound": 1,
                    "limit": 100,
                    "offset": 0,
                    "content": [
                        {
                            "id": "42",
                            "name": "Platform Engineer",
                            "location": {"fullLocation": "Remote"},
                            "ref": detail_url,
                        }
                    ]
                },
                detail_url: {
                    "id": "42",
                    "name": "Platform Engineer",
                    "location": {"fullLocation": "Remote"},
                    "jobAd": {
                        "sections": {
                            "jobDescription": {"text": "Contract Linux platform"}
                        }
                    },
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/42-platform",
                },
            }
        )

        result = collect_sources(
            {"smartrecruiters": [{"tenant": "acme", "company": "Acme"}]},
            client,
        )

        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(
            result.jobs[0].apply_url,
            "https://jobs.smartrecruiters.com/acme/42-platform",
        )
        self.assertEqual(result.jobs[0].description, "Contract Linux platform")

    def test_deduplication_tracks_urls_from_dropped_identity_duplicates(self):
        list_url = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        client = StubClient(
            {
                list_url: [
                    {
                        "id": "duplicate-id",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/first",
                        "applyUrl": "https://jobs.lever.co/acme/first/apply",
                    },
                    {
                        "id": "duplicate-id",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/alias",
                        "applyUrl": "https://jobs.lever.co/acme/shared",
                    },
                    {
                        "id": "third-id",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/third",
                        "applyUrl": "https://jobs.lever.co/acme/shared",
                    },
                ]
            }
        )

        result = collect_sources(
            {"lever": [{"tenant": "acme", "company": "Acme"}]}, client
        )

        self.assertEqual([job.source_id for job in result.jobs], ["duplicate-id"])

    def test_deduplicates_same_application_url_across_sources(self):
        shared_apply = "https://jobs.lever.co/shared/platform"
        client = StubClient(
            {
                "https://api.lever.co/v0/postings/one?mode=json&limit=100&skip=0": [
                    {
                        "id": "one",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/one/platform",
                        "applyUrl": shared_apply,
                    }
                ],
                "https://api.lever.co/v0/postings/two?mode=json&limit=100&skip=0": [
                    {
                        "id": "two",
                        "text": "Platform Engineer",
                        "hostedUrl": "https://jobs.lever.co/two/platform",
                        "applyUrl": shared_apply,
                    }
                ],
            }
        )

        result = collect_sources(
            {
                "lever": [
                    {"tenant": "one", "company": "Acme"},
                    {"tenant": "two", "company": "Acme"},
                ]
            },
            client,
        )

        self.assertEqual(len(result.jobs), 1)

    def test_deduplicates_same_canonical_url_across_tenants(self):
        raw_job = {
            "id": 3,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "Contract Linux role",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/platform-3",
        }
        client = StubClient(
            {
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {
                    "jobs": [raw_job]
                },
                "https://boards-api.greenhouse.io/v1/boards/acme-alias/jobs?content=true": {
                    "jobs": [raw_job]
                },
            }
        )

        result = collect_sources(
            {
                "greenhouse": [
                    {"tenant": "acme", "company": "Acme"},
                    {"tenant": "acme-alias", "company": "Acme"},
                ]
            },
            client,
        )

        self.assertEqual(len(result.jobs), 1)

    def test_fetches_independent_tenants_concurrently(self):
        barrier = threading.Barrier(2, timeout=0.25)

        class BarrierClient:
            def get(self, url, *, headers=None):
                barrier.wait()
                tenant = "one" if "/one/" in url else "two"
                return JsonResult(
                    data={
                        "jobs": [
                            {
                                "id": tenant,
                                "title": "Platform Engineer",
                                "location": {"name": "Remote"},
                                "absolute_url": f"https://example.com/{tenant}",
                                "content": "Contract Linux",
                            }
                        ]
                    },
                    from_cache=False,
                    status=200,
                )

        config = {
            "greenhouse": [
                {"tenant": "one", "company": "One"},
                {"tenant": "two", "company": "Two"},
            ]
        }

        result = collect_sources(config, BarrierClient(), max_workers=2)

        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.failures, [])


if __name__ == "__main__":
    unittest.main()
