import unittest

from job_scout.collector import collect_sources
from job_scout.http import JsonResult, TextResult
from job_scout.linkedin import linkedin_search_url, parse_linkedin_search_page


JOB_ID = "4444718517"


def linkedin_card(
    *,
    job_id=JOB_ID,
    title="Ansible Engineer",
    company="Acme",
    location="United States",
    posted="2026-09-04",
    href=None,
):
    href = href or (
        f"https://www.linkedin.com/jobs/view/ansible-engineer-at-acme-{job_id}"
        "?position=1&pageNum=0&trackingId=secret"
    )
    return f'''<li><div class="base-card job-search-card"
      data-entity-urn="urn:li:jobPosting:{job_id}">
      <a class="base-card__full-link" href="{href}"><span class="sr-only">{title}</span></a>
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle"><a>{company}</a></h4>
      <span class="job-search-card__location">{location}</span>
      <time class="job-search-card__listdate" datetime="{posted}">3 days ago</time>
    </div></li>'''


class StubClient:
    def __init__(self, html):
        self.html = html
        self.text_urls = []

    def get_text(self, url, *, headers=None):
        self.text_urls.append(url)
        outcome = self.html[url]
        if isinstance(outcome, Exception):
            raise outcome
        return TextResult(text=outcome, from_cache=False, status=200)

    def get(self, url, *, headers=None):
        return JsonResult(data={}, from_cache=False, status=200)


class LinkedInParserTests(unittest.TestCase):
    def test_parses_public_guest_card_as_unverified_discovery_candidate(self):
        jobs = parse_linkedin_search_page(linkedin_card(), tenant="us-remote-contract")

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.source, "linkedin")
        self.assertEqual(job.source_id, JOB_ID)
        self.assertEqual(job.title, "Ansible Engineer")
        self.assertEqual(job.company, "Acme")
        self.assertEqual(job.location, "United States")
        self.assertEqual(job.posted_at, "2026-09-04")
        self.assertIsNone(job.remote)
        self.assertIsNone(job.employment_type)
        self.assertEqual(
            job.canonical_url,
            f"https://www.linkedin.com/jobs/view/ansible-engineer-at-acme-{JOB_ID}",
        )
        self.assertEqual(job.apply_url, job.canonical_url)

    def test_rejects_foreign_or_mismatched_job_links(self):
        bad_links = [
            f"https://attacker.example/jobs/view/ansible-engineer-{JOB_ID}",
            "https://www.linkedin.com/jobs/view/ansible-engineer-9999999999",
            "http://www.linkedin.com/jobs/view/ansible-engineer-4444718517",
        ]
        for href in bad_links:
            with self.subTest(href=href):
                with self.assertRaisesRegex(ValueError, "job URL"):
                    parse_linkedin_search_page(linkedin_card(href=href), tenant="x")

    def test_rejects_block_pages_but_accepts_empty_end_page(self):
        with self.assertRaisesRegex(ValueError, "blocked or unexpected"):
            parse_linkedin_search_page("<html>Sign in to continue</html>", tenant="x")
        self.assertEqual(parse_linkedin_search_page("", tenant="x"), [])


class LinkedInCollectorTests(unittest.TestCase):
    def test_uses_guest_remote_contract_filters_and_paginates_by_25(self):
        first = linkedin_search_url("ansible engineer", start=0)
        second = linkedin_search_url("ansible engineer", start=25)
        client = StubClient({first: linkedin_card(), second: ""})

        result = collect_sources(
            {
                "linkedin": [
                    {
                        "tenant": "us-remote-contract",
                        "queries": ["ansible engineer"],
                        "max_pages": 2,
                        "max_results": 100,
                    }
                ]
            },
            client,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual([job.source_id for job in result.jobs], [JOB_ID])
        self.assertEqual(client.text_urls, [first, second])
        self.assertIn("f_WT=2", first)
        self.assertIn("f_JT=C", first)
        self.assertIn("f_TPR=r1209600", first)


if __name__ == "__main__":
    unittest.main()
