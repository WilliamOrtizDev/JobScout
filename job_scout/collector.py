from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
from typing import Any, Callable, Protocol

from .dice import collect_dice_entry
from .http import JsonResult, TextResult
from .models import Candidate, canonical_url_key
from .sources import (
    normalize_ashby,
    normalize_greenhouse,
    normalize_lever,
    normalize_smartrecruiters,
)


@dataclass(frozen=True)
class SourceFailure:
    source: str
    tenant: str
    error: str


@dataclass(frozen=True)
class CollectionResult:
    jobs: list[Candidate]
    failures: list[SourceFailure]
    configured_sources: int
    successful_sources: int


Normalizer = Callable[..., Candidate]


class HttpClient(Protocol):
    def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> JsonResult: ...

    def get_text(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> TextResult: ...


def _greenhouse(tenant: str) -> tuple[str, Callable[[Any], list[dict]], Normalizer]:
    return (
        f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true",
        lambda data: data.get("jobs", []),
        normalize_greenhouse,
    )


def _lever(tenant: str) -> tuple[str, Callable[[Any], list[dict]], Normalizer]:
    return (
        f"https://api.lever.co/v0/postings/{tenant}?mode=json&limit=100&skip=0",
        lambda data: data,
        normalize_lever,
    )


def _ashby(tenant: str) -> tuple[str, Callable[[Any], list[dict]], Normalizer]:
    return (
        f"https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true",
        lambda data: data.get("jobs", []),
        normalize_ashby,
    )


def _smartrecruiters(
    tenant: str,
) -> tuple[str, Callable[[Any], list[dict]], Normalizer]:
    return (
        f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings?limit=100",
        lambda data: data.get("content", []),
        normalize_smartrecruiters,
    )


_BUILDERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
}
_MAX_SMARTRECRUITERS_POSTINGS = 10_000


def _required_int(value: Any, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


_MAX_SMARTRECRUITERS_PAGES = 100
_LEVER_PAGE_SIZE = 100
_MAX_LEVER_PAGES = 100


def _collect_entry(
    source: str,
    entry: Any,
    client: HttpClient,
    title_filter: Callable[[str], bool] | None = None,
) -> tuple[list[Candidate], list[SourceFailure], bool]:
    if not isinstance(entry, dict):
        return (
            [],
            [SourceFailure(source, "<invalid>", "source entry must be an object")],
            False,
        )
    tenant = str(entry.get("tenant") or "<missing>")
    company = str(entry.get("company") or tenant)
    if source == "dice":
        if tenant == "<missing>":
            return [], [SourceFailure(source, tenant, "missing tenant")], False
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tenant) is None:
            return [], [SourceFailure(source, tenant, "invalid tenant")], False
        try:
            return (
                collect_dice_entry(entry, client, title_filter=title_filter),
                [],
                True,
            )
        except Exception as error:
            return [], [SourceFailure(source, tenant, str(error))], False
    builder = _BUILDERS.get(source)
    if builder is None:
        return [], [SourceFailure(source, tenant, "unsupported source")], False
    if tenant == "<missing>":
        return [], [SourceFailure(source, tenant, "missing tenant")], False
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tenant) is None:
        return [], [SourceFailure(source, tenant, "invalid tenant")], False

    failures: list[SourceFailure] = []
    source_succeeded = True
    try:
        url, rows, normalize = builder(tenant)
        if source == "greenhouse" and title_filter is not None:
            url = url.split("?", 1)[0]
        response = client.get(url)
        raw_rows = rows(response.data)
        if not isinstance(raw_rows, list):
            raise ValueError("feed content must be a list")
    except Exception as error:
        return [], [SourceFailure(source=source, tenant=tenant, error=str(error))], False

    if source == "lever":
        if len(raw_rows) > _LEVER_PAGE_SIZE:
            return (
                [],
                [SourceFailure(source, tenant, "page exceeds requested limit")],
                False,
            )
        posting_ids = {
            str(raw["id"])
            for raw in raw_rows
            if isinstance(raw, dict) and raw.get("id") is not None
        }
        page_count = 1
        page_size = len(raw_rows)
        offset = len(raw_rows)
        while page_size == _LEVER_PAGE_SIZE:
            if page_count >= _MAX_LEVER_PAGES:
                failures.append(
                    SourceFailure(source, tenant, "pagination page limit exceeded")
                )
                source_succeeded = False
                break
            page_url = (
                f"https://api.lever.co/v0/postings/{tenant}"
                f"?mode=json&limit={_LEVER_PAGE_SIZE}&skip={offset}"
            )
            try:
                page_rows = rows(client.get(page_url).data)
                if not isinstance(page_rows, list):
                    raise ValueError("feed content must be a list")
                if len(page_rows) > _LEVER_PAGE_SIZE:
                    return (
                        [],
                        failures
                        + [
                            SourceFailure(
                                source,
                                tenant,
                                f"pagination at {offset}: page exceeds requested limit",
                            )
                        ],
                        False,
                    )
            except Exception as error:
                failures.append(
                    SourceFailure(source, tenant, f"pagination at {offset}: {error}")
                )
                source_succeeded = False
                break
            page_posting_ids = [
                str(raw["id"])
                for raw in page_rows
                if isinstance(raw, dict) and raw.get("id") is not None
            ]
            if (
                len(page_posting_ids) != len(set(page_posting_ids))
                or not posting_ids.isdisjoint(page_posting_ids)
            ):
                return (
                    [],
                    failures
                    + [
                        SourceFailure(
                            source,
                            tenant,
                            f"pagination at {offset}: duplicate posting across pages",
                        )
                    ],
                    False,
                )
            posting_ids.update(page_posting_ids)
            raw_rows.extend(page_rows)
            page_size = len(page_rows)
            offset += page_size
            page_count += 1

    if source == "smartrecruiters":
        try:
            total = _required_int(response.data["totalFound"], "totalFound")
            page_size = _required_int(response.data["limit"], "limit")
            response_offset = _required_int(response.data["offset"], "offset")
            if (
                total < 0
                or total > _MAX_SMARTRECRUITERS_POSTINGS
                or page_size < 1
                or page_size > 100
                or response_offset != 0
            ):
                raise ValueError("values outside supported bounds")
            if len(raw_rows) > page_size or len(raw_rows) > total:
                raise ValueError("page exceeds declared pagination bounds")
        except (KeyError, TypeError, ValueError) as error:
            return (
                [],
                [SourceFailure(source, tenant, f"invalid pagination metadata: {error}")],
                False,
            )
        posting_ids = [
            str(raw["id"])
            for raw in raw_rows
            if isinstance(raw, dict) and raw.get("id") is not None
        ]
        if len(posting_ids) != len(set(posting_ids)):
            return (
                [],
                [SourceFailure(source, tenant, "duplicate posting in initial page")],
                False,
            )
        seen_posting_ids = set(posting_ids)
        offset = response_offset + len(raw_rows)
        page_count = 1
        while offset < total:
            if page_count >= _MAX_SMARTRECRUITERS_PAGES:
                failures.append(
                    SourceFailure(source, tenant, "pagination page limit exceeded")
                )
                source_succeeded = False
                break
            page_url = f"{url}&offset={offset}"
            try:
                page_data = client.get(page_url).data
                page_rows = rows(page_data)
                page_total = _required_int(page_data["totalFound"], "totalFound")
                page_limit = _required_int(page_data["limit"], "limit")
                page_offset = _required_int(page_data["offset"], "offset")
                if (
                    page_total != total
                    or page_limit != page_size
                    or page_offset != offset
                ):
                    raise ValueError("inconsistent pagination metadata")
            except Exception as error:
                failures.append(
                    SourceFailure(source, tenant, f"pagination at {offset}: {error}")
                )
                source_succeeded = False
                break
            if not isinstance(page_rows, list):
                return (
                    [],
                    failures
                    + [
                        SourceFailure(
                            source,
                            tenant,
                            f"pagination at {offset}: feed content must be a list",
                        )
                    ],
                    False,
                )
            page_posting_ids = [
                str(raw["id"])
                for raw in page_rows
                if isinstance(raw, dict) and raw.get("id") is not None
            ]
            if (
                len(page_posting_ids) != len(set(page_posting_ids))
                or not seen_posting_ids.isdisjoint(page_posting_ids)
            ):
                return (
                    [],
                    failures
                    + [
                        SourceFailure(
                            source,
                            tenant,
                            f"pagination at {offset}: duplicate posting across pages",
                        )
                    ],
                    False,
                )
            seen_posting_ids.update(page_posting_ids)
            if (
                len(page_rows) > page_size
                or len(raw_rows) + len(page_rows) > total
                or len(raw_rows) + len(page_rows) > _MAX_SMARTRECRUITERS_POSTINGS
            ):
                return (
                    [],
                    failures
                    + [
                        SourceFailure(
                            source,
                            tenant,
                            f"pagination at {offset}: page exceeds declared bounds",
                        )
                    ],
                    False,
                )
            if not page_rows:
                failures.append(
                    SourceFailure(source, tenant, f"pagination at {offset}: empty page")
                )
                source_succeeded = False
                break
            raw_rows.extend(page_rows)
            offset += len(page_rows)
            page_count += 1
        if len(raw_rows) != total:
            failures.append(
                SourceFailure(
                    source,
                    tenant,
                    f"incomplete pagination: expected {total} rows, received {len(raw_rows)}",
                )
            )
            source_succeeded = False

    valid_rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if isinstance(raw, dict):
            valid_rows.append(raw)
        else:
            failures.append(
                SourceFailure(
                    source,
                    tenant,
                    "job unknown: normalization failed: source row must be an object",
                )
            )
    raw_rows = valid_rows

    if source in {"greenhouse", "smartrecruiters"} and title_filter is not None:
        title_fields = ("title",) if source == "greenhouse" else ("name", "title")
        raw_rows = [
            raw
            for raw in raw_rows
            if title_filter(
                str(next((raw.get(field) for field in title_fields if raw.get(field)), ""))
            )
        ]

    detail_rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        try:
            detail_url = None
            if source == "greenhouse" and title_filter is not None:
                detail_url = f"{url}/{raw['id']}"
            elif source == "smartrecruiters" and not raw.get("applyUrl"):
                detail_url = (
                    "https://api.smartrecruiters.com/v1/companies/"
                    f"{tenant}/postings/{raw['id']}"
                )
            detail = client.get(detail_url).data if detail_url else raw
            if not isinstance(detail, dict):
                raise ValueError("detail must be an object")
            detail_rows.append(detail)
        except Exception as error:
            job_id = raw.get("id") or raw.get("uuid") or "unknown"
            failures.append(
                SourceFailure(source, tenant, f"job {job_id}: {error}")
            )

    jobs: list[Candidate] = []
    for raw in detail_rows:
        try:
            jobs.append(normalize(raw, tenant=tenant, company=company))
        except Exception as error:
            job_id = raw.get("id") or raw.get("uuid") or "unknown"
            failures.append(
                SourceFailure(source, tenant, f"job {job_id}: normalization failed: {error}")
            )
    return jobs, failures, source_succeeded


def collect_sources(
    config: dict[str, Any],
    client: HttpClient,
    *,
    max_workers: int = 8,
    title_filter: Callable[[str], bool] | None = None,
) -> CollectionResult:
    if not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be between 1 and 32")
    jobs: list[Candidate] = []
    failures: list[SourceFailure] = []
    seen_ids: set[tuple[str, str, str]] = set()
    seen_urls: set[str] = set()
    entries: list[tuple[str, Any]] = []
    configured_sources = 0
    for source, source_entries in config.items():
        if not isinstance(source_entries, list):
            configured_sources += 1
            failures.append(
                SourceFailure(source, "<invalid>", "source entries must be a list")
            )
            continue
        configured_sources += len(source_entries)
        entries.extend((source, entry) for entry in source_entries)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(
            lambda item: _collect_entry(
                item[0], item[1], client, title_filter=title_filter
            ),
            entries,
        )
    successful_sources = 0
    for collected_jobs, entry_failures, source_succeeded in results:
        failures.extend(entry_failures)
        successful_sources += int(source_succeeded)
        for job in collected_jobs:
            identity = (job.source, job.tenant, job.source_id)
            url_keys = {
                canonical_url_key(job.canonical_url),
                canonical_url_key(job.apply_url),
            }
            is_new = identity not in seen_ids and url_keys.isdisjoint(seen_urls)
            if is_new:
                jobs.append(job)
            seen_ids.add(identity)
            seen_urls.update(url_keys)
    return CollectionResult(
        jobs=jobs,
        failures=failures,
        configured_sources=configured_sources,
        successful_sources=successful_sources,
    )
