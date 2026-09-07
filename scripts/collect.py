#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.http import JsonHttpClient
from job_scout.collector import collect_sources
from job_scout.community_client import CommunityDatasetClient
from job_scout.community_sources import collect_community_sources, merge_collection_results
from job_scout.filtering import title_matches
from job_scout.pipeline import run_collected_pipeline, write_result
from job_scout.state_store import SQLiteStateStore


def configured_source_count(source_config) -> int:
    if not isinstance(source_config, dict):
        raise ValueError("source configuration must be an object")
    return sum(
        len(entries) if isinstance(entries, list) else 1
        for entries in source_config.values()
    )


def run_pipeline(
    source_config,
    profile,
    client,
    *,
    known_records,
    community_config=None,
    community_client=None,
):
    direct = collect_sources(
        source_config,
        client,
        title_filter=lambda title: title_matches(title, profile),
    )
    if community_config is None:
        combined = direct
    else:
        if community_client is None:
            raise ValueError("community_client is required when community sources are configured")
        community = collect_community_sources(
            community_config,
            community_client,
            titles=profile.get("targets", {}).get("titles", []),
            posted_within_days=profile.get("targets", {}).get(
                "posted_within_days_preferred", 14
            ),
        )
        combined = merge_collection_results(direct, community)
    return run_collected_pipeline(combined, profile, known_records=known_records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect structured ATS job postings")
    parser.add_argument("--sources", type=Path, default=ROOT / "config/sources.json")
    parser.add_argument(
        "--community-sources",
        type=Path,
        default=ROOT / "config/community-sources.json",
    )
    parser.add_argument("--profile", type=Path, default=ROOT / "config/search-profile.json")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/candidates.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "runtime/http-cache")

    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "runtime/job-scout.db",
        help="SQLite state database (created and migrated automatically)",
    )
    args = parser.parse_args()

    source_config = json.loads(args.sources.read_text())
    community_config = json.loads(args.community_sources.read_text())
    profile = json.loads(args.profile.read_text())
    try:
        configured = configured_source_count(source_config) + configured_source_count(
            community_config
        )
    except ValueError as error:
        parser.error(str(error))
    if configured == 0:
        parser.error("no source tenants are configured")

    store = SQLiteStateStore(args.database)
    with store.run_lock():
        result = run_pipeline(
            source_config,
            profile,
            JsonHttpClient(args.cache),
            known_records=store.application_dedupe_entries(),
            community_config=community_config,
            community_client=CommunityDatasetClient(
                cache_dir=args.cache / "community"
            ),
        )
        write_result(result, args.output)
        run_id = store.record_pipeline_result(result)
    print(
        json.dumps(
            {
                "configured_tenants": configured,
                "successful_sources": result.successful_sources,
                "accepted": len(result.jobs),
                "source_failures": len(result.source_failures),
                "rejected": result.rejected_counts,
                "output": str(args.output),
                "database": str(args.database),
                "run_id": run_id,
            },
            sort_keys=True,
        )
    )
    return 1 if result.successful_sources == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
