import unittest

from job_scout.collector import collect_sources
from job_scout.dice import parse_dice_search_page
from job_scout.http import JsonResult, TextResult


GUID = "c35aee0c-f71b-4e1d-af15-fe97877f99e0"


def dice_page(*cards: str) -> str:
    return (
        '<!doctype html><html><body>'
        '<div data-testid="job-search-split-view-card-list-scroll">'
        '<div role="list" aria-label="Job search results">'
        + "".join(cards)
        + "</div></div></body></html>"
    )


def dice_card(
    *,
    guid: str = GUID,
    title: str = "Linux/Axway",
    company: str = "ERPA",
    location: str = "Remote",
    posted: str = "2d ago",
    employment: str = "Contract, Third Party",
    salary: str | None = "$60 - $70",
    href: str | None = None,
) -> str:
    salary_html = (
        f'<p id="salary-label">{salary}</p>' if salary is not None else ""
    )
    return f"""
    <div role="listitem">
      <div data-job-guid="{guid}" data-testid="job-card" role="article">
        <a data-testid="job-search-job-detail-link" aria-label="{title}"
           href="{href or f'/job-detail/{guid}'}">{title}</a>
        <p data-testid="job-card-company-name">{company}</p>
        <p class="mb-0 text-sm font-normal text-foreground-light">
          {location} <!-- --> • <!-- --> {posted}
        </p>
        <p id="employmentType-label">{employment}</p>
        {salary_html}
      </div>
    </div>
    """


class StubClient:
    def __init__(self, *, html=None, json=None):
        self.html = html or {}
        self.json = json or {}
        self.text_urls = []

    def get_text(self, url, *, headers=None):
        self.text_urls.append(url)
        outcome = self.html[url]
        if isinstance(outcome, Exception):
            raise outcome
        return TextResult(text=outcome, from_cache=False, status=200)

    def get(self, url, *, headers=None):
        outcome = self.json[url]
        if isinstance(outcome, Exception):
            raise outcome
        return JsonResult(data=outcome, from_cache=False, status=200)


class DiceParserTests(unittest.TestCase):
    def test_parses_server_rendered_card_into_candidate(self):
        jobs = parse_dice_search_page(dice_page(dice_card()), tenant="us-remote-contract")

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.source, "dice")
        self.assertEqual(job.source_id, GUID)
        self.assertEqual(job.title, "Linux/Axway")
        self.assertEqual(job.company, "ERPA")
        self.assertEqual(job.location, "Remote")
        self.assertEqual(job.employment_type, "Contract, Third Party")
        self.assertTrue(job.remote)
        self.assertEqual(job.posted_at, "2d ago")
        self.assertEqual(job.compensation, "$60 - $70")
        self.assertEqual(job.canonical_url, f"https://www.dice.com/job-detail/{GUID}")
        self.assertEqual(job.apply_url, job.canonical_url)

    def test_ignores_similar_job_cards_outside_search_results(self):
        page = dice_page(dice_card())
        similar = dice_card(company="", location="")
        page = page.replace("</body>", f"<aside>{similar}</aside></body>")

        jobs = parse_dice_search_page(page, tenant="us-remote-contract")

        self.assertEqual([job.source_id for job in jobs], [GUID])

    def test_rejects_external_or_mismatched_detail_links(self):
        for href in (
            "https://attacker.example/job-detail/" + GUID,
            "/job-detail/00000000-0000-0000-0000-000000000000",
            f"/job-detail/{GUID}?redirect=https://attacker.example",
        ):
            with self.subTest(href=href):
                with self.assertRaisesRegex(ValueError, "unexpected detail URL"):
                    parse_dice_search_page(
                        dice_page(dice_card(href=href)), tenant="us-remote-contract"
                    )

    def test_rejects_blocked_or_truncated_pages(self):
        pages = (
            "<html><body>Access denied</body></html>",
            dice_page(dice_card())[:-20],
        )
        for page in pages:
            with self.subTest(page=page[:30]):
                with self.assertRaisesRegex(ValueError, "blocked|malformed"):
                    parse_dice_search_page(page, tenant="us-remote-contract")


class DiceCollectorTests(unittest.TestCase):
    def test_queries_exact_filters_paginates_and_deduplicates_across_searches(self):
        guid2 = "0e555169-70ed-4ca5-9a42-340147178998"
        linux1 = (
            "https://www.dice.com/jobs?q=linux&countryCode=US&page=1&pageSize=100"
            "&filters.employmentType=CONTRACTS&filters.workplaceTypes=Remote&language=en"
        )
        linux2 = linux1.replace("page=1", "page=2")
        sre1 = linux1.replace("q=linux", "q=site+reliability")
        sre2 = sre1.replace("page=1", "page=2")
        client = StubClient(
            html={
                linux1: dice_page(dice_card()),
                linux2: dice_page(),
                sre1: dice_page(
                    dice_card(),
                    dice_card(
                        guid=guid2,
                        title="Site Reliability Engineer",
                        company="Acme",
                        salary=None,
                    ),
                ),
                sre2: dice_page(),
            }
        )

        result = collect_sources(
            {
                "dice": [
                    {
                        "tenant": "us-remote-contract",
                        "queries": ["linux", "site reliability"],
                        "page_size": 100,
                        "max_pages": 2,
                        "max_results": 10,
                    }
                ]
            },
            client,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual([job.source_id for job in result.jobs], [GUID, guid2])
        self.assertEqual(client.text_urls, [linux1, linux2, sre1, sre2])
        self.assertIsNone(result.jobs[1].compensation)

    def test_malformed_later_page_fails_closed_without_aborting_other_sources(self):
        dice1 = (
            "https://www.dice.com/jobs?q=linux&countryCode=US&page=1&pageSize=100"
            "&filters.employmentType=CONTRACTS&filters.workplaceTypes=Remote&language=en"
        )
        dice2 = dice1.replace("page=1", "page=2")
        lever = "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0"
        client = StubClient(
            html={dice1: dice_page(dice_card()), dice2: "<html>Access denied</html>"},
            json={lever: []},
        )

        result = collect_sources(
            {
                "dice": [
                    {
                        "tenant": "us-remote-contract",
                        "queries": ["linux"],
                        "max_pages": 2,
                    }
                ],
                "lever": [{"tenant": "acme", "company": "Acme"}],
            },
            client,
        )

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.successful_sources, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].source, "dice")
        self.assertIn("page 2", result.failures[0].error)

    def test_max_results_stops_collection_at_configured_bound(self):
        url = (
            "https://www.dice.com/jobs?q=linux&countryCode=US&page=1&pageSize=100"
            "&filters.employmentType=CONTRACTS&filters.workplaceTypes=Remote&language=en"
        )
        guid2 = "0e555169-70ed-4ca5-9a42-340147178998"
        client = StubClient(
            html={url: dice_page(dice_card(), dice_card(guid=guid2))}
        )

        result = collect_sources(
            {
                "dice": [
                    {
                        "tenant": "us-remote-contract",
                        "queries": ["linux"],
                        "max_pages": 10,
                        "max_results": 1,
                    }
                ]
            },
            client,
        )

        self.assertEqual(result.successful_sources, 1)
        self.assertEqual([job.source_id for job in result.jobs], [GUID])
        self.assertEqual(client.text_urls, [url])


if __name__ == "__main__":
    unittest.main()
