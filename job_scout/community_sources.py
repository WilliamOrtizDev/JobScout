from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Protocol

from .collector import CollectionResult, SourceFailure
from .models import Candidate, canonical_url_key
from .sources import public_https_url


class CommunityClient(Protocol):
    def fetch_ats_scrapers(
        self, entry: dict[str, Any], *, cutoff: datetime, titles: list[str]
    ) -> list[dict[str, Any]]: ...

    def fetch_openroles(
        self, entry: dict[str, Any], *, cutoff: datetime, titles: list[str]
    ) -> list[dict[str, Any]]: ...


_SUPPORTED_SOURCES = ("ats_scrapers", "openroles")
_OPENROLES_FILE = re.compile(r"slim/slim-[A-Za-z0-9._-]+\.json\.gz\Z")
_SHORT_SHA256 = re.compile(r"[0-9a-f]{16}\Z")


def _required_text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _remote_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "remote"}:
        return True
    if normalized in {"false", "0", "no", "onsite", "on-site", "hybrid"}:
        return False
    return None


def _normalize_ats_scrapers(row: dict[str, Any]) -> Candidate:
    canonical_url = public_https_url(_required_text(row, "url"))
    apply_url = public_https_url(_optional_text(row.get("apply_url")) or canonical_url)
    ats_type = _required_text(row, "ats_type")
    source_id = _optional_text(row.get("ats_id")) or _optional_text(
        row.get("requisition_id")
    )
    if source_id is None:
        raise ValueError("ats_id or requisition_id must be a non-empty string")
    description = _optional_text(row.get("description")) or ""
    return Candidate(
        source="ats_scrapers",
        tenant=ats_type,
        source_id=source_id,
        company=_required_text(row, "company"),
        title=_required_text(row, "title"),
        location=_optional_text(row.get("location")),
        description=description,
        employment_type=(
            _optional_text(row.get("employment_type") or row.get("commitment"))
            or ""
        ).replace("_", " ")
        or None,
        remote=_remote_value(row.get("is_remote")),
        posted_at=_optional_text(row.get("posted_at")),
        canonical_url=canonical_url,
        apply_url=apply_url,
        raw=row,
    )


def _normalize_openroles(row: dict[str, Any]) -> Candidate:
    if row.get("s") not in {None, 0, False}:
        raise ValueError("stale row")
    ats = _required_text(row, "a")
    tenant = _required_text(row, "t")
    url = public_https_url(_required_text(row, "u"))
    workplace = _optional_text(row.get("w"))
    raw = dict(row)
    raw["compensation_min"] = row.get("cm")
    raw["compensation_max"] = row.get("cmax")
    raw["compensation_currency"] = row.get("cur")
    return Candidate(
        source="openroles",
        tenant=f"{ats}:{tenant}",
        source_id=_required_text(row, "i"),
        company=_required_text(row, "c"),
        title=_required_text(row, "ti"),
        location=_optional_text(row.get("loc")),
        description="",
        employment_type=None,
        remote=_remote_value(workplace),
        posted_at=_optional_text(row.get("p")),
        canonical_url=url,
        apply_url=url,
        raw=raw,
    )


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def select_openroles_chunks(
    manifest: dict[str, Any],
    *,
    cutoff: datetime,
    max_chunks: int,
    max_compressed_bytes: int,
    max_raw_bytes: int = 256 * 1024 * 1024,
    max_candidate_rows: int = 320_000,
) -> list[dict[str, Any]]:
    if manifest.get("slim_index_schema_version") != "1.0":
        raise ValueError("unsupported openroles slim-index schema")
    chunks = manifest.get("slim_index_chunks")
    if not isinstance(chunks, list):
        raise ValueError("openroles slim_index_chunks must be a list")
    if not 1 <= max_chunks <= 128:
        raise ValueError("max_chunks must be between 1 and 128")
    if not 1 <= max_compressed_bytes <= 100 * 1024 * 1024:
        raise ValueError("invalid compressed byte budget")
    if not 1 <= max_raw_bytes <= 512 * 1024 * 1024:
        raise ValueError("invalid raw byte budget")
    if not 1 <= max_candidate_rows <= 1_000_000:
        raise ValueError("invalid candidate row budget")

    selected: list[dict[str, Any]] = []
    total_bytes = 0
    total_raw_bytes = 0
    total_rows = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("openroles chunk must be an object")
        posted_max = chunk.get("posted_max")
        if posted_max is None or _parse_timestamp(posted_max, "posted_max") < cutoff:
            continue
        filename = chunk.get("file")
        digest = chunk.get("sha")
        rows = chunk.get("rows")
        compressed = chunk.get("bytes_gz")
        raw_bytes = chunk.get("bytes_raw")
        if (
            not isinstance(filename, str)
            or _OPENROLES_FILE.fullmatch(filename) is None
            or not isinstance(digest, str)
            or _SHORT_SHA256.fullmatch(digest) is None
            or not isinstance(rows, int)
            or not 0 <= rows <= 20_000
            or not isinstance(compressed, int)
            or compressed < 1
            or not isinstance(raw_bytes, int)
            or raw_bytes < 1
            or raw_bytes > 16 * 1024 * 1024
        ):
            raise ValueError("invalid openroles chunk metadata")
        total_bytes += compressed
        if total_bytes > max_compressed_bytes:
            raise ValueError("openroles chunks exceed compressed byte budget")
        total_raw_bytes += raw_bytes
        if total_raw_bytes > max_raw_bytes:
            raise ValueError("openroles chunks exceed raw byte budget")
        total_rows += rows
        if total_rows > max_candidate_rows:
            raise ValueError("openroles chunks exceed candidate row budget")
        selected.append(chunk)
        if len(selected) > max_chunks:
            raise ValueError("openroles chunks exceed chunk budget")
    return selected


def collect_community_sources(
    config: dict[str, Any],
    client: CommunityClient,
    *,
    titles: list[str],
    posted_within_days: int,
    now: datetime | None = None,
) -> CollectionResult:
    if not isinstance(config, dict):
        raise ValueError("community source configuration must be an object")
    if not titles or any(not isinstance(title, str) or not title.strip() for title in titles):
        raise ValueError("titles must be a non-empty list of strings")
    if not 1 <= posted_within_days <= 90:
        raise ValueError("posted_within_days must be between 1 and 90")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = current.astimezone(timezone.utc) - timedelta(days=posted_within_days)

    jobs: list[Candidate] = []
    failures: list[SourceFailure] = []
    seen_urls: set[str] = set()
    seen_identities: set[tuple[str, str, str]] = set()
    configured = 0
    successful = 0
    normalizers = {
        "ats_scrapers": _normalize_ats_scrapers,
        "openroles": _normalize_openroles,
    }

    for source in _SUPPORTED_SOURCES:
        entries = config.get(source, [])
        if not isinstance(entries, list):
            configured += 1
            failures.append(SourceFailure(source, "<invalid>", "source entries must be a list"))
            continue
        for raw_entry in entries:
            configured += 1
            if not isinstance(raw_entry, dict):
                failures.append(SourceFailure(source, "<invalid>", "source entry must be an object"))
                continue
            name = str(raw_entry.get("name") or "<missing>")
            try:
                if source == "ats_scrapers":
                    rows = client.fetch_ats_scrapers(
                        raw_entry, cutoff=cutoff, titles=titles
                    )
                else:
                    rows = client.fetch_openroles(
                        raw_entry, cutoff=cutoff, titles=titles
                    )
                if not isinstance(rows, list):
                    raise ValueError("feed content must be a list")
                successful += 1
            except Exception as error:
                failures.append(SourceFailure(source, name, str(error)))
                continue
            normalization_error_count = 0
            first_normalization_error = ""
            for row in rows:
                if not isinstance(row, dict):
                    normalization_error_count += 1
                    first_normalization_error = (
                        first_normalization_error or "source row must be an object"
                    )
                    continue
                try:
                    job = normalizers[source](row)
                except Exception as error:
                    normalization_error_count += 1
                    first_normalization_error = first_normalization_error or str(error)
                    continue
                url_keys = {
                    canonical_url_key(job.canonical_url),
                    canonical_url_key(job.apply_url),
                }
                identity = (job.source, job.company.casefold(), job.source_id)
                if url_keys.isdisjoint(seen_urls) and identity not in seen_identities:
                    jobs.append(job)
                seen_urls.update(url_keys)
                seen_identities.add(identity)
            if normalization_error_count:
                failures.append(
                    SourceFailure(
                        source,
                        name,
                        f"normalization failed for {normalization_error_count} rows; first error: {first_normalization_error}",
                    )
                )

    return CollectionResult(
        jobs=jobs,
        failures=failures,
        configured_sources=configured,
        successful_sources=successful,
    )


def merge_collection_results(*results: CollectionResult) -> CollectionResult:
    jobs: list[Candidate] = []
    failures: list[SourceFailure] = []
    seen_urls: set[str] = set()
    seen_identities: set[tuple[str, str, str]] = set()
    configured = 0
    successful = 0
    for result in results:
        configured += result.configured_sources
        successful += result.successful_sources
        failures.extend(result.failures)
        for job in result.jobs:
            url_keys = {
                canonical_url_key(job.canonical_url),
                canonical_url_key(job.apply_url),
            }
            identity = (job.source, job.company.casefold(), job.source_id)
            if url_keys.isdisjoint(seen_urls) and identity not in seen_identities:
                jobs.append(job)
            seen_urls.update(url_keys)
            seen_identities.add(identity)
    return CollectionResult(jobs, failures, configured, successful)
