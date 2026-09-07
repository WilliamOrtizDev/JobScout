from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
import json
import os
import tempfile

from .collector import CollectionResult, SourceFailure, canonical_url_key, collect_sources
from .filtering import prefilter, title_matches
from .http import JsonHttpClient
from .models import Candidate


@dataclass(frozen=True)
class PipelineResult:
    jobs: list[Candidate]
    source_failures: list[SourceFailure]
    rejected_counts: dict[str, int]
    configured_sources: int
    successful_sources: int


def run_pipeline(
    source_config: dict[str, Any],
    profile: dict[str, Any],
    client: JsonHttpClient,
    *,
    known_urls: set[str] | None = None,
    known_records: Mapping[str, str | None]
    | Iterable[tuple[str, str | None]]
    | None = None,
) -> PipelineResult:
    collected = collect_sources(
        source_config,
        client,
        title_filter=lambda title: title_matches(title, profile),
    )
    return run_collected_pipeline(
        collected,
        profile,
        known_urls=known_urls,
        known_records=known_records,
    )


def run_collected_pipeline(
    collected: CollectionResult,
    profile: dict[str, Any],
    *,
    known_urls: set[str] | None = None,
    known_records: Mapping[str, str | None]
    | Iterable[tuple[str, str | None]]
    | None = None,
) -> PipelineResult:
    accepted: list[Candidate] = []
    rejected: Counter[str] = Counter()
    normalized_known: dict[str, set[str | None]] = {}
    known_entries: Iterable[tuple[str, str | None]]
    if isinstance(known_records, Mapping):
        known_entries = cast(Mapping[str, str | None], known_records).items()
    else:
        known_entries = known_records or ()
    for url, content_hash in known_entries:
        normalized_known.setdefault(canonical_url_key(url), set()).add(content_hash)
    for url in known_urls or set():
        normalized_known.setdefault(canonical_url_key(url), set()).add(None)
    for job in collected.jobs:
        current_hash = str(job.to_dict()["content_hash"])
        url_keys = {
            canonical_url_key(job.canonical_url),
            canonical_url_key(job.apply_url),
        }
        matching_hashes = {
            known_hash
            for url_key in url_keys
            for known_hash in normalized_known.get(url_key, set())
        }
        if matching_hashes and any(
            known_hash is None or known_hash == current_hash
            for known_hash in matching_hashes
        ):
            rejected["seen"] += 1
            continue
        decision = prefilter(job, profile)
        if decision.keep:
            accepted.append(job)
        else:
            rejected.update(decision.reasons)
    return PipelineResult(
        jobs=accepted,
        source_failures=collected.failures,
        rejected_counts=dict(rejected),
        configured_sources=collected.configured_sources,
        successful_sources=collected.successful_sources,
    )


def write_result(result: PipelineResult, output: Path) -> None:
    value = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "counts": {
            "accepted": len(result.jobs),
            "configured_sources": result.configured_sources,
            "successful_sources": result.successful_sources,
            "source_failures": len(result.source_failures),
            "rejected": result.rejected_counts,
        },
        "source_failures": [asdict(failure) for failure in result.source_failures],
        "jobs": [job.to_dict() for job in result.jobs],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
