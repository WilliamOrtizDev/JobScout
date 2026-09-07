from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.request
from unittest import mock
import job_scout.community_client as community_client_module

from job_scout.community_client import (
    CommunityDatasetClient,
    _SafeRedirectHandler,
    community_python_executable,
    query_ats_parquet,
)


class CommunityDatasetClientTests(unittest.TestCase):
    def test_ats_downloads_and_verifies_bounded_partition_before_local_query(self):
        body = b"verified parquet fixture"
        digest = hashlib.sha256(body).hexdigest()
        partition_url = "https://storage.stapply.ai/jobhive/v1/paylocity/jobs.parquet"
        manifest = {
            "version": "2.0",
            "by_ats": {
                "paylocity": {
                    "parquet": partition_url,
                    "parquet_sha256": digest,
                    "parquet_size_bytes": len(body),
                    "rows": 1,
                }
            },
        }
        queried = []

        def query(path, *args):
            local = Path(path)
            self.assertFalse(str(path).startswith("https://"))
            self.assertEqual(local.read_bytes(), body)
            queried.append(local)
            return [{"ats_id": "1"}]

        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=lambda *_: manifest,
                binary_fetch=lambda url, limit: body,
                parquet_query=query,
                cache_dir=cache,
            )
            result = client.fetch_ats_scrapers(
                {
                    "manifest_url": "https://storage.stapply.ai/jobhive/v1/manifest.json",
                    "ats_types": ["paylocity"],
                    "max_files": 1,
                    "max_dataset_bytes": len(body),
                },
                cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                titles=["DevOps Engineer"],
            )

        self.assertEqual(result, [{"ats_id": "1"}])
        self.assertEqual(len(queried), 1)

    def test_ats_manifest_and_partition_cache_avoid_refetch_within_ttl(self):
        body = b"parquet fixture"
        manifest = {
            "version": "2.0",
            "by_ats": {
                "paylocity": {
                    "parquet": "paylocity/jobs.parquet",
                    "parquet_sha256": hashlib.sha256(body).hexdigest(),
                    "parquet_size_bytes": len(body),
                }
            },
        }
        calls = {"manifest": 0, "partition": 0}

        def json_fetch(*_):
            calls["manifest"] += 1
            return manifest

        def binary_fetch(*_):
            calls["partition"] += 1
            return body

        entry = {
            "manifest_url": "https://storage.stapply.ai/jobhive/v1/manifest.json",
            "ats_types": ["paylocity"],
            "manifest_cache_seconds": 21600,
        }
        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=json_fetch,
                binary_fetch=binary_fetch,
                parquet_query=lambda *args: [],
                cache_dir=cache,
            )
            for _ in range(2):
                client.fetch_ats_scrapers(
                    entry,
                    cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    titles=["DevOps Engineer"],
                )

        self.assertEqual(calls, {"manifest": 1, "partition": 1})

    def test_future_dated_manifest_cache_is_refetched(self):
        calls = 0

        def json_fetch(*_):
            nonlocal calls
            calls += 1
            return {"version": "2.0", "by_ats": {}}

        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=json_fetch,
                binary_fetch=lambda *args: b"",
                parquet_query=lambda *args: [],
                cache_dir=cache,
            )
            client._ats_manifest(
                "https://storage.stapply.ai/jobhive/v1/manifest.json", 21600
            )
            manifest_path = Path(cache) / "ats-scrapers-manifest.json"
            future = community_client_module.time.time() + 60
            os.utime(manifest_path, (future, future))
            client._ats_manifest(
                "https://storage.stapply.ai/jobhive/v1/manifest.json", 21600
            )

        self.assertEqual(calls, 2)

    def test_runtime_honors_configured_community_venv(self):
        executable = community_python_executable(
            Path("/automation"),
            {"JOB_SCOUT_COMMUNITY_VENV": "/custom/community"},
            path_exists=lambda path: path == Path("/custom/community/bin/python"),
        )

        self.assertEqual(executable, "/custom/community/bin/python")

    def test_chunk_cache_uses_lock_and_random_temporary_file(self):
        rows = [{"i": "1", "ti": "DevOps Engineer", "p": "2026-09-05T00:00:00Z"}]
        raw = json.dumps(rows).encode()
        compressed = gzip.compress(raw)
        chunk = {
            "sha": hashlib.sha256(raw).hexdigest()[:16], "rows": 1,
            "bytes_gz": len(compressed), "bytes_raw": len(raw),
        }
        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=lambda *_: {}, binary_fetch=lambda *_: compressed,
                parquet_query=lambda *args, **kwargs: [], cache_dir=cache,
            )
            real_named_temporary_file = tempfile.NamedTemporaryFile
            with mock.patch.object(community_client_module.fcntl, "flock") as flock, mock.patch.object(
                community_client_module.tempfile,
                "NamedTemporaryFile",
                wraps=real_named_temporary_file,
            ) as named_temporary_file:
                client._chunk_rows("https://openroles.today/data/slim/chunk.json.gz", chunk)

        flock.assert_called()
        named_temporary_file.assert_called_once()

    def test_openroles_enforces_cutoff_for_each_row(self):
        rows = [
            {"i": "recent", "ti": "DevOps Engineer", "p": "2026-09-05T00:00:00Z"},
            {"i": "old", "ti": "DevOps Engineer", "p": "2026-08-01T00:00:00Z"},
        ]
        raw = json.dumps(rows).encode()
        compressed = gzip.compress(raw)
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [{
                "file": "slim/slim-new.json.gz",
                "sha": hashlib.sha256(raw).hexdigest()[:16],
                "rows": 2,
                "bytes_gz": len(compressed),
                "bytes_raw": len(raw),
                "posted_max": "2026-09-06T00:00:00Z",
            }],
        }
        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=lambda *_: manifest,
                binary_fetch=lambda *_: compressed,
                parquet_query=lambda *args, **kwargs: [],
                cache_dir=cache,
            )
            result = client.fetch_openroles(
                {"manifest_url": "https://openroles.today/data/manifest.json"},
                cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                titles=["DevOps Engineer"],
            )

        self.assertEqual([row["i"] for row in result], ["recent"])

    def test_duckdb_disables_external_access_for_local_partition(self):
        class FakeConnection:
            description = []

            def __init__(self):
                self.queries = []

            def execute(self, query, parameters=None):
                self.queries.append((query, parameters))
                return self

            def fetchall(self):
                return []

            def close(self):
                pass

        connection = FakeConnection()
        fake_duckdb = mock.Mock(connect=mock.Mock(return_value=connection))
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "jobs.parquet"
            dataset.write_bytes(b"fixture")
            with mock.patch.dict("sys.modules", {"duckdb": fake_duckdb}):
                query_ats_parquet(
                    str(dataset),
                    datetime(2026, 8, 23, tzinfo=timezone.utc),
                    ["DevOps Engineer"],
                    10,
                    True,
                    ["US"],
                )

        statements = [query.strip() for query, _ in connection.queries]
        self.assertIn("SET autoinstall_known_extensions = false", statements)
        self.assertIn("SET autoload_known_extensions = false", statements)
        allowed_path_calls = [
            parameters
            for query, parameters in connection.queries
            if query.strip() == "SET allowed_paths = ?"
        ]
        self.assertEqual(allowed_path_calls, [[[str(dataset.resolve())]]])
        self.assertIn("SET enable_external_access = false", statements)
        self.assertNotIn("LOAD httpfs", statements)

    def test_redirect_handler_rejects_cross_origin_before_following(self):
        handler = _SafeRedirectHandler("https://openroles.today")
        request = urllib.request.Request("https://openroles.today/data/manifest.json")

        with self.assertRaisesRegex(ValueError, "cross-origin redirect"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.invalid/chunk.json.gz",
            )

    def test_fetches_verified_openroles_chunk(self):
        rows = [
            {"i": "1", "ti": "DevOps Engineer", "w": "remote", "cc": "US", "p": "2026-09-05T00:00:00Z"},
            {"i": "2", "ti": "Frontend Engineer", "w": "remote", "cc": "US", "p": "2026-09-05T00:00:00Z"},
            {"i": "3", "ti": "DevOps Engineer", "w": "onsite", "cc": "US", "p": "2026-09-05T00:00:00Z"},
            {"i": "4", "ti": "DevOps Engineer", "w": "remote", "cc": "CA", "p": "2026-09-05T00:00:00Z"},
        ]
        raw = json.dumps(rows).encode("utf-8")
        compressed = gzip.compress(raw)
        digest = hashlib.sha256(raw).hexdigest()[:16]
        manifest_url = "https://openroles.today/data/manifest.json"
        chunk_url = "https://openroles.today/data/slim/slim-new.json.gz"
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [
                {
                    "file": "slim/slim-new.json.gz",
                    "sha": digest,
                    "rows": 4,
                    "bytes_gz": len(compressed),
                    "bytes_raw": len(raw),
                    "posted_min": "2026-09-05T00:00:00Z",
                    "posted_max": "2026-09-06T00:00:00Z",
                    "has_null_posted": False,
                }
            ],
        }
        json_calls = []
        binary_calls = []

        def fetch_json(url, max_bytes):
            json_calls.append((url, max_bytes))
            return manifest

        def fetch_binary(url, max_bytes):
            binary_calls.append((url, max_bytes))
            return compressed

        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=fetch_json,
                binary_fetch=fetch_binary,
                parquet_query=lambda *args, **kwargs: [],
                cache_dir=cache,
            )
            result = client.fetch_openroles(
                {
                    "manifest_url": manifest_url,
                    "max_chunks": 16,
                    "max_compressed_bytes": 32 * 1024 * 1024,
                    "require_remote": True,
                    "country_codes": ["US"],
                },
                cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                titles=["DevOps Engineer"],
            )

        self.assertEqual(result, rows[:1])
        self.assertEqual(json_calls[0][0], manifest_url)
        self.assertEqual(binary_calls[0][0], chunk_url)

    def test_rejects_openroles_chunk_with_bad_digest(self):
        raw = b"[]"
        compressed = gzip.compress(raw)
        manifest = {
            "slim_index_schema_version": "1.0",
            "slim_index_chunks": [
                {
                    "file": "slim/slim-new.json.gz",
                    "sha": "0" * 16,
                    "rows": 0,
                    "bytes_gz": len(compressed),
                    "bytes_raw": len(raw),
                    "posted_min": "2026-09-05T00:00:00Z",
                    "posted_max": "2026-09-06T00:00:00Z",
                    "has_null_posted": False,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=lambda *_: manifest,
                binary_fetch=lambda *_: compressed,
                parquet_query=lambda *args, **kwargs: [],
                cache_dir=cache,
            )
            with self.assertRaisesRegex(ValueError, "digest"):
                client.fetch_openroles(
                    {"manifest_url": "https://openroles.today/data/manifest.json"},
                    cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    titles=["DevOps Engineer"],
                )
            self.assertEqual(list(Path(cache).glob("*.json.gz")), [])

    def test_rejects_cross_origin_dataset_reference(self):
        body = b"fixture"
        manifest = {
            "version": "2.0",
            "by_ats": {
                "paylocity": {
                    "parquet": "https://attacker.example/jobs.parquet",
                    "parquet_sha256": hashlib.sha256(body).hexdigest(),
                    "parquet_size_bytes": len(body),
                }
            },
        }
        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=lambda *_: manifest,
                binary_fetch=lambda *args, **kwargs: body,
                parquet_query=lambda *args, **kwargs: [],
                cache_dir=cache,
            )
            with self.assertRaisesRegex(ValueError, "same origin"):
                client.fetch_ats_scrapers(
                    {
                        "manifest_url": "https://storage.stapply.ai/jobhive/v1/manifest.json",
                        "ats_types": ["paylocity"],
                    },
                    cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    titles=["DevOps Engineer"],
                )

    def test_rejects_unpinned_same_origin_ats_partition_reference(self):
        body = b"fixture"
        manifest = {
            "version": "2.0",
            "by_ats": {
                "paylocity": {
                    "parquet": "other.parquet",
                    "parquet_sha256": hashlib.sha256(body).hexdigest(),
                    "parquet_size_bytes": len(body),
                }
            },
        }
        with tempfile.TemporaryDirectory() as cache:
            client = CommunityDatasetClient(
                json_fetch=lambda *_: manifest,
                binary_fetch=lambda *args, **kwargs: body,
                parquet_query=lambda *args, **kwargs: [],
                cache_dir=cache,
            )
            with self.assertRaisesRegex(ValueError, "partition URL is not pinned"):
                client.fetch_ats_scrapers(
                    {
                        "manifest_url": "https://storage.stapply.ai/jobhive/v1/manifest.json",
                        "ats_types": ["paylocity"],
                    },
                    cutoff=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    titles=["DevOps Engineer"],
                )


if __name__ == "__main__":
    unittest.main()
