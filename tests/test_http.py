import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from job_scout.http import JsonHttpClient


class FakeResponse:
    def __init__(self, payload, *, status=200, headers=None):
        self.payload = json.dumps(payload).encode()
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, amount=None):
        return self.payload if amount is None else self.payload[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SequenceOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class JsonHttpClientTests(unittest.TestCase):
    def test_rejects_invalid_client_limits(self):
        invalid_options = (
            {"timeout": 0},
            {"timeout": 301},
            {"retries": -1},
            {"retries": 11},
            {"max_response_bytes": 0},
            {"max_response_bytes": 100 * 1024 * 1024 + 1},
        )
        with tempfile.TemporaryDirectory() as directory:
            for options in invalid_options:
                with self.subTest(options=options):
                    with self.assertRaisesRegex(ValueError, "must be"):
                        JsonHttpClient(Path(directory), **options)

    def test_repeated_304_after_invalid_cache_is_bounded(self):
        responses = [
            HTTPError("https://example.test/jobs", 304, "", Message(), None),
            HTTPError("https://example.test/jobs", 304, "", Message(), None),
        ]
        opener = SequenceOpener(responses)
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.write_text("not-json")
            meta_path.write_text(json.dumps({"etag": '"v1"'}))

            with self.assertRaises(HTTPError):
                client.get("https://example.test/jobs")

        self.assertEqual(len(opener.requests), 2)

    def test_refetches_when_cached_body_exceeds_response_limit_after_304(self):
        not_modified = HTTPError(
            "https://example.test/jobs", 304, "", Message(), None
        )
        opener = SequenceOpener([not_modified, FakeResponse({"jobs": [2]})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener, max_response_bytes=64)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.write_text(json.dumps({"jobs": ["x" * 100]}))
            meta_path.write_text(json.dumps({"etag": '"v1"'}))

            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [2]})
        self.assertEqual(len(opener.requests), 2)

    def test_refetches_when_cached_body_is_corrupt_after_304(self):
        not_modified = HTTPError(
            "https://example.test/jobs", 304, "", Message(), None
        )
        opener = SequenceOpener([not_modified, FakeResponse({"jobs": [2]})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.write_text("not-json")
            meta_path.write_text('{"etag":"v1"}')

            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [2]})
        self.assertEqual(len(opener.requests), 2)
        self.assertIsNone(opener.requests[1].get_header("If-none-match"))

    def test_prunes_oldest_cache_entries_and_lock_files(self):
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), max_cache_entries=2)
            entries = []
            for index in range(3):
                body_path, meta_path = client._paths(f"https://example.test/jobs/{index}")
                lock_path = body_path.with_suffix(".lock")
                body_path.write_text("{}")
                meta_path.write_text("{}")
                lock_path.write_text("")
                timestamp = time.time() + index
                for path in (body_path, meta_path, lock_path):
                    os.utime(path, (timestamp, timestamp))
                entries.append((body_path, meta_path, lock_path))

            client.prune()

            self.assertTrue(all(not path.exists() for path in entries[0]))
            self.assertTrue(all(path.exists() for entry in entries[1:] for path in entry))

    def test_discards_non_string_cache_validators(self):
        opener = SequenceOpener([FakeResponse({"jobs": [2]})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.write_text('{"jobs":[1]}')
            meta_path.write_text(json.dumps({"etag": [], "last_modified": 42}))

            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [2]})
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))
        self.assertIsNone(opener.requests[0].get_header("If-modified-since"))

    def test_recovers_from_oversized_cache_metadata(self):
        opener = SequenceOpener([FakeResponse({"jobs": [2]})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.write_text('{"jobs":[1]}')
            meta_path.write_text(json.dumps({"etag": "x" * (65 * 1024)}))

            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [2]})
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))

    def test_recovers_from_wrong_shaped_cache_metadata(self):
        opener = SequenceOpener([FakeResponse({"jobs": [2]})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.write_text('{"jobs":[1]}')
            meta_path.write_text("[]")

            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [2]})
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))

    def test_recovers_from_malformed_cache_metadata(self):
        opener = SequenceOpener([FakeResponse({"jobs": [2]})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            body_path, meta_path = client._paths("https://example.test/jobs")
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text('{"jobs":[1]}')
            meta_path.write_text("not-json")

            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [2]})
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))

    def test_retries_network_timeout(self):
        opener = SequenceOpener(
            [URLError(TimeoutError("timed out")), FakeResponse({"jobs": [1]})]
        )
        delays = []
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(
                Path(directory), opener=opener, retries=1, sleeper=delays.append
            )
            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [1]})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(delays, [0.5])

    def test_retries_transient_http_failure(self):
        retry_headers = Message()
        retry_headers["Retry-After"] = "0"
        unavailable = HTTPError(
            "https://example.test/jobs", 503, "Unavailable", retry_headers, None
        )
        opener = SequenceOpener([unavailable, FakeResponse({"jobs": [1]})])
        delays = []
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(
                Path(directory), opener=opener, retries=1, sleeper=delays.append
            )
            result = client.get("https://example.test/jobs")

        self.assertEqual(result.data, {"jobs": [1]})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(delays, [0.0])

    def test_rejects_response_larger_than_configured_limit(self):
        opener = SequenceOpener([FakeResponse({"description": "x" * 100})])
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(
                Path(directory), opener=opener, max_response_bytes=32
            )
            with self.assertRaisesRegex(ValueError, "response exceeds"):
                client.get("https://example.test/jobs")

    def test_default_client_rejects_cross_host_redirects(self):
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory))
            handlers = [
                item
                for item in getattr(client.opener, "handlers", [])
                if item.__class__.__name__ == "SafeRedirectHandler"
            ]
            self.assertEqual(len(handlers), 1)
            handler = handlers[0]
            original = Request("https://api.lever.co/v0/postings/acme")
            with self.assertRaises(HTTPError):
                handler.redirect_request(
                    original,
                    None,
                    302,
                    "Found",
                    {},
                    "http://127.0.0.1/metadata",
                )

    def test_serializes_same_url_cache_updates(self):
        class TrackingOpener:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.guard = threading.Lock()

            def open(self, request, timeout):
                with self.guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.05)
                with self.guard:
                    self.active -= 1
                return FakeResponse({"jobs": [request.full_url]})

        opener = TrackingOpener()
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        client.get,
                        ["https://example.test/jobs", "https://example.test/jobs"],
                    )
                )

        self.assertEqual(len(results), 2)
        self.assertEqual(opener.max_active, 1)

    def test_sends_if_modified_since_when_etag_is_unavailable(self):
        not_modified = HTTPError("https://example.test/jobs", 304, "", Message(), None)
        opener = SequenceOpener(
            [
                FakeResponse(
                    {"jobs": [1]},
                    headers={"Last-Modified": "Sat, 05 Sep 2026 12:00:00 GMT"},
                ),
                not_modified,
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            client.get("https://example.test/jobs")
            second = client.get("https://example.test/jobs")

        self.assertTrue(second.from_cache)
        self.assertEqual(
            opener.requests[1].get_header("If-modified-since"),
            "Sat, 05 Sep 2026 12:00:00 GMT",
        )

    def test_reuses_cached_json_when_server_returns_304(self):
        not_modified = HTTPError("https://example.test/jobs", 304, "", Message(), None)
        opener = SequenceOpener(
            [FakeResponse({"jobs": [1]}, headers={"ETag": '"v1"'}), not_modified]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(Path(directory), opener=opener)
            first = client.get("https://example.test/jobs")
            second = client.get("https://example.test/jobs")

        self.assertEqual(first.data, {"jobs": [1]})
        self.assertFalse(first.from_cache)
        self.assertEqual(second.data, first.data)
        self.assertTrue(second.from_cache)
        self.assertEqual(opener.requests[1].get_header("If-none-match"), '"v1"')


if __name__ == "__main__":
    unittest.main()
