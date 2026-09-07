from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from .models import Candidate


_LINKEDIN_ORIGIN = "https://www.linkedin.com"
_LINKEDIN_SEARCH = f"{_LINKEDIN_ORIGIN}/jobs-guest/jobs/api/seeMoreJobPostings/search"
_SPACE = re.compile(r"\s+")
_JOB_URN = re.compile(r"urn:li:jobPosting:(\d{6,20})\Z")
_MAX_QUERIES = 20
_MAX_PAGES = 10
_MAX_RESULTS = 1000
_PAGE_SIZE = 25
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


def _classes(attrs: dict[str, str | None]) -> set[str]:
    return set((attrs.get("class") or "").split())


def _clean(parts: list[str]) -> str:
    return _SPACE.sub(" ", " ".join(parts)).strip()


class _LinkedInParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.card: dict[str, Any] | None = None
        self.card_depth = 0
        self.captures: list[tuple[str, int, list[str]]] = []
        self.cards: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        classes = _classes(attrs_dict)
        if self.card is None:
            if tag == "div" and "job-search-card" in classes:
                self.card = {"urn": attrs_dict.get("data-entity-urn")}
                self.card_depth = 1
            return

        if tag not in _VOID_TAGS:
            self.card_depth += 1
        capture = None
        if tag == "a" and "base-card__full-link" in classes:
            self.card["href"] = attrs_dict.get("href")
        elif tag == "h3" and "base-search-card__title" in classes:
            capture = "title"
        elif tag == "h4" and "base-search-card__subtitle" in classes:
            capture = "company"
        elif "job-search-card__location" in classes:
            capture = "location"
        elif tag == "time" and "job-search-card__listdate" in classes:
            self.card["posted_at"] = attrs_dict.get("datetime")
        if capture:
            self.captures.append((capture, self.card_depth, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for _name, _depth, parts in self.captures:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.card is None:
            return
        remaining = []
        for name, depth, parts in self.captures:
            if depth == self.card_depth:
                value = _clean(parts)
                if value:
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


def _required(card: dict[str, Any], name: str) -> str:
    value = card.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"LinkedIn card missing {name}")
    return value.strip()


def _job_url(href: str, job_id: str) -> str:
    parts = urlsplit(href)
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme != "https"
        or host != "www.linkedin.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or not parts.path.startswith("/jobs/view/")
        or not re.search(rf"-{re.escape(job_id)}/?\Z", parts.path)
    ):
        raise ValueError("LinkedIn card has unexpected job URL")
    path = parts.path.rstrip("/")
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def parse_linkedin_search_page(html: str, *, tenant: str) -> list[Candidate]:
    if not isinstance(html, str):
        raise ValueError("LinkedIn response must be text")
    if not html.strip():
        return []
    parser = _LinkedInParser()
    parser.feed(html)
    parser.close()
    if parser.card is not None:
        raise ValueError("malformed LinkedIn HTML")
    if not parser.cards:
        raise ValueError("blocked or unexpected LinkedIn HTML")

    jobs = []
    for card in parser.cards:
        urn = _required(card, "urn")
        match = _JOB_URN.fullmatch(urn)
        if match is None:
            raise ValueError("LinkedIn card has invalid job URN")
        job_id = match.group(1)
        url = _job_url(_required(card, "href"), job_id)
        jobs.append(
            Candidate(
                source="linkedin",
                tenant=tenant,
                source_id=job_id,
                company=_required(card, "company"),
                title=_required(card, "title"),
                location=_required(card, "location"),
                description="",
                employment_type=None,
                remote=None,
                posted_at=_required(card, "posted_at"),
                canonical_url=url,
                apply_url=url,
                raw={
                    "job_id": job_id,
                    "search_remote_filter": True,
                    "search_contract_filter": True,
                },
            )
        )
    return jobs


def linkedin_search_url(query: str, *, start: int) -> str:
    params = [
        ("keywords", query),
        ("location", "United States"),
        ("f_WT", "2"),
        ("f_JT", "C"),
        ("f_TPR", "r1209600"),
        ("start", str(start)),
    ]
    return f"{_LINKEDIN_SEARCH}?{urlencode(params)}"


def _bounded_int(entry: dict[str, Any], name: str, default: int, maximum: int) -> int:
    value = entry.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def collect_linkedin_entry(entry: dict[str, Any], client: Any, *, title_filter=None) -> list[Candidate]:
    tenant = entry["tenant"]
    queries = entry.get("queries")
    if (
        not isinstance(queries, list)
        or not queries
        or len(queries) > _MAX_QUERIES
        or any(not isinstance(q, str) or not q.strip() or len(q) > 100 for q in queries)
    ):
        raise ValueError(f"queries must contain between 1 and {_MAX_QUERIES} non-empty strings")
    max_pages = _bounded_int(entry, "max_pages", 4, _MAX_PAGES)
    max_results = _bounded_int(entry, "max_results", 500, _MAX_RESULTS)

    jobs: list[Candidate] = []
    seen: set[str] = set()
    for query_value in queries:
        query = query_value.strip()
        for page in range(max_pages):
            url = linkedin_search_url(query, start=page * _PAGE_SIZE)
            try:
                page_jobs = parse_linkedin_search_page(
                    client.get_text(url).text, tenant=tenant
                )
            except Exception as error:
                raise ValueError(f"query {query!r} page {page + 1}: {error}") from error
            new_on_page = 0
            for job in page_jobs:
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                new_on_page += 1
                if title_filter is None or title_filter(job.title):
                    jobs.append(job)
                    if len(jobs) >= max_results:
                        return jobs
            if not page_jobs or new_on_page == 0:
                break
    return jobs
