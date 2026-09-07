from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlencode
from uuid import UUID

from .models import Candidate


_DICE_ORIGIN = "https://www.dice.com"
_SPACE = re.compile(r"\s+")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_MAX_QUERIES = 20
_MAX_PAGES = 10
_MAX_PAGE_SIZE = 100
_MAX_RESULTS = 1_000


def _text(parts: list[str]) -> str:
    return _SPACE.sub(" ", " ".join(parts)).strip()


class _DiceSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_results_container = False
        self.results_depth = 0
        self.html_depth = 0
        self.closed_html = False
        self.card: dict[str, Any] | None = None
        self.card_depth = 0
        self.captures: list[tuple[str, int, list[str]]] = []
        self.cards: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag == "html":
            self.html_depth += 1
        if self.results_depth and tag not in _VOID_TAGS:
            self.results_depth += 1
        elif attrs.get("data-testid") == "job-search-split-view-card-list-scroll":
            self.has_results_container = True
            self.results_depth = 1

        if self.card is None:
            if self.results_depth and attrs.get("data-testid") == "job-card":
                self.card = {"guid": attrs.get("data-job-guid")}
                self.card_depth = 1
            return

        if tag not in _VOID_TAGS:
            self.card_depth += 1
        capture: str | None = None
        if attrs.get("data-testid") == "job-search-job-detail-link":
            self.card["href"] = attrs.get("href")
            aria_label = attrs.get("aria-label")
            if aria_label:
                self.card["title"] = aria_label
            capture = "title"
        elif attrs.get("data-testid") == "job-card-company-name":
            capture = "company"
        elif attrs.get("id") == "employmentType-label":
            capture = "employment_type"
        elif attrs.get("id") == "salary-label":
            capture = "compensation"
        elif tag == "p" and {
            "text-sm",
            "font-normal",
            "text-foreground-light",
        }.issubset(set((attrs.get("class") or "").split())):
            capture = "location_posted"
        if capture is not None:
            self.captures.append((capture, self.card_depth, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for _name, _depth, parts in self.captures:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.card is not None:
            remaining: list[tuple[str, int, list[str]]] = []
            for name, depth, parts in self.captures:
                if depth == self.card_depth:
                    value = _text(parts)
                    if value and (name != "title" or not self.card.get("title")):
                        self.card[name] = value
                else:
                    remaining.append((name, depth, parts))
            self.captures = remaining
            if self.card_depth == 1:
                self.cards.append(self.card)
                self.card = None
                self.card_depth = 0
                self.captures = []
            elif tag not in _VOID_TAGS:
                self.card_depth -= 1
        if tag == "html" and self.html_depth:
            self.html_depth -= 1
            if self.html_depth == 0:
                self.closed_html = True
        if self.results_depth and tag not in _VOID_TAGS:
            self.results_depth -= 1


def _canonical_guid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("job card missing GUID")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("job card has invalid GUID") from None
    if normalized != value.lower():
        raise ValueError("job card has non-canonical GUID")
    return normalized


def _required_card_text(card: dict[str, Any], name: str) -> str:
    value = card.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"job card missing {name.replace('_', ' ')}")
    return value.strip()


def parse_dice_search_page(html: str, *, tenant: str) -> list[Candidate]:
    if not isinstance(html, str) or not html:
        raise ValueError("Dice response must be non-empty HTML")
    parser = _DiceSearchParser()
    parser.feed(html)
    parser.close()
    if not parser.closed_html or parser.card is not None:
        raise ValueError("malformed Dice HTML")
    if not parser.has_results_container:
        raise ValueError("blocked or unexpected Dice HTML")

    jobs: list[Candidate] = []
    for card in parser.cards:
        guid = _canonical_guid(card.get("guid"))
        title = _required_card_text(card, "title")
        company = _required_card_text(card, "company")
        employment_type = _required_card_text(card, "employment_type")
        if "contract" not in employment_type.lower():
            raise ValueError("Dice job card is not a contract result")
        location_posted = _required_card_text(card, "location_posted")
        if "•" not in location_posted:
            raise ValueError("job card missing relative posting indicator")
        location, posted_at = (part.strip() for part in location_posted.rsplit("•", 1))
        if not location or not posted_at:
            raise ValueError("job card has invalid location or posting indicator")
        if "remote" not in location.lower():
            raise ValueError("Dice job card is not remote")
        expected_path = f"/job-detail/{guid}"
        href = _required_card_text(card, "href")
        parts = urlsplit(href)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or parts.path != expected_path:
            raise ValueError("job card has unexpected detail URL")
        url = f"{_DICE_ORIGIN}{expected_path}"
        compensation = card.get("compensation")
        if compensation is not None and not isinstance(compensation, str):
            raise ValueError("job card has invalid compensation")
        jobs.append(
            Candidate(
                source="dice",
                tenant=tenant,
                source_id=guid,
                company=company,
                title=title,
                location=location,
                description="",
                employment_type=employment_type,
                remote=True,
                posted_at=posted_at,
                canonical_url=url,
                apply_url=url,
                raw={
                    "guid": guid,
                    "compensation": compensation,
                    "relative_posted": posted_at,
                },
                compensation=compensation,
            )
        )
    return jobs


def _bounded_int(entry: dict[str, Any], name: str, default: int, maximum: int) -> int:
    value = entry.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def dice_search_url(query: str, *, page: int, page_size: int) -> str:
    params = [
        ("q", query),
        ("countryCode", "US"),
        ("page", str(page)),
        ("pageSize", str(page_size)),
        ("filters.employmentType", "CONTRACTS"),
        ("filters.workplaceTypes", "Remote"),
        ("language", "en"),
    ]
    return f"{_DICE_ORIGIN}/jobs?{urlencode(params)}"


def collect_dice_entry(entry: dict[str, Any], client: Any, *, title_filter=None) -> list[Candidate]:
    tenant = entry["tenant"]
    queries = entry.get("queries")
    if (
        not isinstance(queries, list)
        or not queries
        or len(queries) > _MAX_QUERIES
        or any(
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 100
            or any(ord(character) < 32 for character in query)
            for query in queries
        )
    ):
        raise ValueError(
            f"queries must contain between 1 and {_MAX_QUERIES} non-empty strings"
        )
    page_size = _bounded_int(entry, "page_size", 100, _MAX_PAGE_SIZE)
    max_pages = _bounded_int(entry, "max_pages", 2, _MAX_PAGES)
    max_results = _bounded_int(entry, "max_results", 500, _MAX_RESULTS)

    jobs: list[Candidate] = []
    seen_guids: set[str] = set()
    for query_value in queries:
        query = query_value.strip()
        for page in range(1, max_pages + 1):
            url = dice_search_url(query, page=page, page_size=page_size)
            try:
                response = client.get_text(url)
                page_jobs = parse_dice_search_page(response.text, tenant=tenant)
            except Exception as error:
                raise ValueError(f"query {query!r} page {page}: {error}") from error
            new_on_page = 0
            for job in page_jobs:
                if job.source_id in seen_guids:
                    continue
                seen_guids.add(job.source_id)
                new_on_page += 1
                if title_filter is None or title_filter(job.title):
                    jobs.append(job)
                    if len(jobs) >= max_results:
                        return jobs
            if not page_jobs or new_on_page == 0:
                break
    return jobs