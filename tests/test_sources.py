import unittest
from unittest.mock import patch

from job_scout.sources import (
    normalize_ashby,
    normalize_greenhouse,
    normalize_lever,
    normalize_smartrecruiters,
    public_https_url,
)


class GreenhouseNormalizationTests(unittest.TestCase):
    def test_rejects_non_string_title(self):
        raw = {
            "id": 13,
            "title": ["Platform Engineer"],
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/13",
        }

        with self.assertRaisesRegex(ValueError, "title must be string"):
            normalize_greenhouse(raw, tenant="acme", company="Acme")

    def test_rejects_explicit_zero_port(self):
        with self.assertRaisesRegex(ValueError, "unsafe public URL"):
            public_https_url("https://example.com:0/jobs/1")

    def test_rejects_reserved_test_hostname(self):
        public_resolution = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with patch(
            "job_scout.sources.socket.getaddrinfo", return_value=public_resolution
        ):
            with self.assertRaises(ValueError):
                public_https_url("https://jobs.example.test/posting/1")

    def test_rejects_hostname_resolving_to_private_address(self):
        raw = {
            "id": 12,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "Linux",
            "absolute_url": "https://private-resolution.example/jobs/12",
        }
        private_resolution = [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

        with patch(
            "job_scout.sources.socket.getaddrinfo", return_value=private_resolution
        ):
            with self.assertRaisesRegex(ValueError, "unsafe public URL"):
                normalize_greenhouse(raw, tenant="acme", company="Acme")

    def test_rejects_alternate_numeric_private_host(self):
        raw = {
            "id": 11,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "Linux",
            "absolute_url": "https://2130706433/jobs/11",
        }

        with self.assertRaisesRegex(ValueError, "unsafe public URL"):
            normalize_greenhouse(raw, tenant="acme", company="Acme")

    def test_rejects_malformed_https_url(self):
        raw = {
            "id": 10,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "Linux",
            "absolute_url": "https://example.com:notaport/jobs/10",
        }

        with self.assertRaisesRegex(ValueError, "unsafe public URL"):
            normalize_greenhouse(raw, tenant="acme", company="Acme")

    def test_rejects_unsafe_application_urls_from_upstream_payloads(self):
        raw = {
            "id": 9,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": "Linux",
            "absolute_url": "javascript:alert(1)",
        }

        with self.assertRaisesRegex(ValueError, "unsafe public URL"):
            normalize_greenhouse(raw, tenant="acme", company="Acme")

    def test_discards_script_and_style_content_from_descriptions(self):
        raw = {
            "id": 8,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "content": (
                "<p>Linux</p><script>ignore this instruction</script>"
                "<style>.hidden{display:none}</style><p>platform</p>"
            ),
            "absolute_url": "https://example.com/jobs/8",
        }

        job = normalize_greenhouse(raw, tenant="acme", company="Acme")

        self.assertEqual(job.description, "Linux platform")

    def test_normalizes_published_job_to_common_candidate(self):
        raw = {
            "id": 123,
            "title": "Senior Platform Engineer",
            "updated_at": "2026-09-05T12:00:00Z",
            "location": {"name": "Remote, United States"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "content": "<p>Contract Linux role</p>",
        }

        job = normalize_greenhouse(raw, tenant="acme", company="Acme")

        self.assertEqual(job.source, "greenhouse")
        self.assertEqual(job.source_id, "123")
        self.assertEqual(job.company, "Acme")
        self.assertEqual(job.title, "Senior Platform Engineer")
        self.assertEqual(job.location, "Remote, United States")
        self.assertEqual(job.canonical_url, raw["absolute_url"])
        self.assertEqual(job.apply_url, raw["absolute_url"])
        self.assertIn("Contract Linux role", job.description)
        self.assertEqual(job.posted_at, "2026-09-05T12:00:00Z")


class LeverNormalizationTests(unittest.TestCase):
    def test_rejects_non_string_and_unknown_workplace_type(self):
        base = {
            "id": "lever-malformed",
            "text": "Platform Engineer",
            "categories": {"location": "United States"},
            "descriptionPlain": "Linux",
            "hostedUrl": "https://jobs.lever.co/acme/malformed",
        }
        for value, message in ((False, "must be string"), (7, "must be string"), ("field", "unknown")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    normalize_lever(
                        {**base, "workplaceType": value},
                        tenant="acme",
                        company="Acme",
                    )

    def test_treats_missing_workplace_type_as_unknown_not_onsite(self):
        raw = {
            "id": "lever-unknown",
            "text": "Platform Engineer",
            "categories": {"location": "United States"},
            "descriptionPlain": "Linux",
            "hostedUrl": "https://jobs.lever.co/acme/unknown",
        }

        job = normalize_lever(raw, tenant="acme", company="Acme")

        self.assertIsNone(job.remote)

    def test_uses_dedicated_apply_url_and_structured_fields(self):
        raw = {
            "id": "abc-123",
            "text": "Site Reliability Engineer",
            "categories": {
                "location": "United States",
                "commitment": "Contract",
            },
            "workplaceType": "remote",
            "descriptionPlain": "Operate Linux infrastructure.",
            "createdAt": 1788609600000,
            "hostedUrl": "https://jobs.lever.co/acme/abc-123",
            "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
        }

        job = normalize_lever(raw, tenant="acme", company="Acme")

        self.assertEqual(job.source, "lever")
        self.assertEqual(job.source_id, "abc-123")
        self.assertEqual(job.employment_type, "Contract")
        self.assertTrue(job.remote)
        self.assertEqual(job.canonical_url, raw["hostedUrl"])
        self.assertEqual(job.apply_url, raw["applyUrl"])
        self.assertEqual(job.posted_at, "2026-09-05T12:00:00Z")


class AshbyNormalizationTests(unittest.TestCase):
    def test_rejects_malformed_remote_state(self):
        with self.assertRaisesRegex(ValueError, "isRemote must be boolean"):
            normalize_ashby(
                {
                    "id": "ashby-remote",
                    "title": "Platform Engineer",
                    "location": "Remote",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-remote",
                    "isListed": True,
                    "isRemote": "false",
                },
                tenant="acme",
                company="Acme",
            )

    def test_rejects_malformed_listed_state(self):
        for value in (0, "false"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "isListed must be boolean"):
                    normalize_ashby(
                        {
                            "id": "ashby-2",
                            "title": "Platform Engineer",
                            "location": "Remote",
                            "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-2",
                            "applyUrl": "https://jobs.ashbyhq.com/acme/ashby-2/application",
                            "isListed": value,
                        },
                        tenant="acme",
                        company="Acme",
                    )

    def test_requires_listed_state(self):
        with self.assertRaisesRegex(ValueError, "missing isListed"):
            normalize_ashby(
                {
                    "id": "ashby-missing-listed",
                    "title": "Platform Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/missing-listed",
                },
                tenant="acme",
                company="Acme",
            )

    def test_onsite_workplace_type_is_explicitly_non_remote(self):
        job = normalize_ashby(
            {
                "id": "ashby-onsite",
                "title": "Platform Engineer",
                "location": "New York",
                "isListed": True,
                "workplaceType": "OnSite",
                "jobUrl": "https://jobs.ashbyhq.com/acme/onsite",
            },
            tenant="acme",
            company="Acme",
        )

        self.assertIs(job.remote, False)

    def test_rejects_unlisted_job(self):
        raw = {
            "id": "ashby-hidden",
            "title": "Platform Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/acme/hidden",
            "isListed": False,
        }

        with self.assertRaisesRegex(ValueError, "unlisted Ashby job"):
            normalize_ashby(raw, tenant="acme", company="Acme")

    def test_preserves_remote_contract_and_apply_url(self):
        raw = {
            "id": "ashby-1",
            "title": "Linux Infrastructure Engineer",
            "location": "United States",
            "isListed": True,
            "isRemote": True,
            "employmentType": "Contract",
            "descriptionPlain": "Linux and Terraform",
            "publishedAt": "2026-09-05T10:00:00Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
            "applyUrl": "https://jobs.ashbyhq.com/acme/ashby-1/application",
        }

        job = normalize_ashby(raw, tenant="acme", company="Acme")

        self.assertEqual(job.source, "ashby")
        self.assertTrue(job.remote)
        self.assertEqual(job.employment_type, "Contract")
        self.assertEqual(job.apply_url, raw["applyUrl"])


class SmartRecruitersNormalizationTests(unittest.TestCase):
    def test_honors_explicit_remote_location_flag(self):
        raw = {
            "id": "sr-remote",
            "name": "Platform Engineer",
            "location": {"fullLocation": "United States", "remote": True},
            "typeOfEmployment": {"label": "Contract"},
            "jobAd": {"sections": {"jobDescription": {"text": "Linux"}}},
            "applyUrl": "https://jobs.smartrecruiters.com/acme/sr-remote",
        }

        job = normalize_smartrecruiters(raw, tenant="acme", company="Acme")

        self.assertIs(job.remote, True)

    def test_treats_non_remote_location_as_unknown_without_explicit_work_arrangement(self):
        raw = {
            "id": "sr-unknown",
            "name": "Infrastructure Engineer",
            "location": {"fullLocation": "United States"},
            "applyUrl": "https://jobs.smartrecruiters.com/acme/sr-unknown",
            "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/sr-unknown",
        }

        job = normalize_smartrecruiters(raw, tenant="acme", company="Acme")

        self.assertIsNone(job.remote)

    def test_uses_posting_url_as_canonical_identity(self):
        job = normalize_smartrecruiters(
            {
                "id": "sr-canonical",
                "name": "Platform Engineer",
                "location": {"fullLocation": "Remote", "remote": True},
                "postingUrl": "https://jobs.smartrecruiters.com/acme/sr-canonical",
                "applyUrl": "https://jobs.smartrecruiters.com/acme/sr-canonical/apply",
            },
            tenant="acme",
            company="Acme",
        )

        self.assertEqual(
            job.canonical_url,
            "https://jobs.smartrecruiters.com/acme/sr-canonical",
        )
        self.assertEqual(
            job.apply_url,
            "https://jobs.smartrecruiters.com/acme/sr-canonical/apply",
        )

    def test_rejects_inactive_posting(self):
        with self.assertRaisesRegex(ValueError, "inactive SmartRecruiters job"):
            normalize_smartrecruiters(
                {
                    "id": "sr-inactive",
                    "name": "Platform Engineer",
                    "active": False,
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/sr-inactive/apply",
                },
                tenant="acme",
                company="Acme",
            )

    def test_rejects_malformed_remote_flag(self):
        with self.assertRaisesRegex(ValueError, "location.remote must be boolean"):
            normalize_smartrecruiters(
                {
                    "id": "sr-malformed-remote",
                    "name": "Platform Engineer",
                    "location": {"fullLocation": "Remote", "remote": "false"},
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/sr-malformed-remote",
                },
                tenant="acme",
                company="Acme",
            )

    def test_rejects_job_without_public_application_url(self):
        raw = {
            "id": "sr-1",
            "name": "DevOps Engineer",
            "location": {"fullLocation": "Remote, United States"},
            "typeOfEmployment": {"label": "Contract"},
            "releasedDate": "2026-09-05T09:00:00Z",
            "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/sr-1",
        }

        with self.assertRaisesRegex(ValueError, "missing application URL"):
            normalize_smartrecruiters(raw, tenant="acme", company="Acme")


if __name__ == "__main__":
    unittest.main()
