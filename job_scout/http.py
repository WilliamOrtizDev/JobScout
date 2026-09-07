from dataclasses import dataclass
import fcntl
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import json
import os
import tempfile
import time


@dataclass(frozen=True)
class JsonResult:
    data: Any
    from_cache: bool
    status: int


@dataclass(frozen=True)
class TextResult:
    text: str
    from_cache: bool
    status: int


class SafeRedirectHandler(HTTPRedirectHandler):
    """Permit redirects only within the original HTTPS origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original = urlsplit(req.full_url)
        destination = urlsplit(newurl)
        original_origin = (original.scheme.lower(), original.hostname, original.port)
        destination_origin = (
            destination.scheme.lower(),
            destination.hostname,
            destination.port,
        )
        if original_origin != destination_origin or destination.scheme.lower() != "https":
            raise HTTPError(newurl, code, "unsafe cross-origin redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class JsonHttpClient:
    def __init__(
        self,
        cache_dir: Path,
        *,
        opener=None,
        timeout: int = 30,
        retries: int = 2,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_cache_entries: int = 5_000,
        max_cache_age_seconds: int = 30 * 24 * 60 * 60,
        sleeper=time.sleep,
    ):
        if not 1 <= timeout <= 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        if not 0 <= retries <= 10:
            raise ValueError("retries must be between 0 and 10")
        if not 1 <= max_response_bytes <= 100 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 and 104857600")
        if not 1 <= max_cache_entries <= 100_000:
            raise ValueError("max_cache_entries must be between 1 and 100000")
        if not 60 <= max_cache_age_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("max_cache_age_seconds must be between 60 and 31536000")
        self.cache_dir = cache_dir
        self.opener = opener or build_opener(SafeRedirectHandler())
        self.timeout = timeout
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self.max_cache_entries = max_cache_entries
        self.max_cache_age_seconds = max_cache_age_seconds
        self._next_prune = 0.0
        self.sleeper = sleeper

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.meta.json"

    def _text_paths(self, url: str) -> tuple[Path, Path]:
        key = sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.html", self.cache_dir / f"{key}.html.meta.json"

    def prune(self) -> None:
        """Bound persistent cache entries while no request is using them."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        global_lock_path = self.cache_dir / ".cache.lock"
        with global_lock_path.open("a+") as global_lock:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX)
            now = time.time()
            body_paths = [
                *[
                    path
                    for path in self.cache_dir.glob("*.json")
                    if not path.name.endswith(".meta.json")
                ],
                *self.cache_dir.glob("*.html"),
            ]
            body_paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            kept_sidecars: set[Path] = set()
            for index, body_path in enumerate(body_paths):
                expired = now - body_path.stat().st_mtime > self.max_cache_age_seconds
                if body_path.suffix == ".html":
                    meta_path = body_path.with_name(f"{body_path.name}.meta.json")
                    lock_path = body_path.with_name(f"{body_path.name}.lock")
                else:
                    meta_path = body_path.with_name(f"{body_path.stem}.meta.json")
                    lock_path = body_path.with_suffix(".lock")
                if index < self.max_cache_entries and not expired:
                    kept_sidecars.update((meta_path, lock_path))
                    continue
                body_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                lock_path.unlink(missing_ok=True)

            for path in self.cache_dir.glob("*.meta.json"):
                if path not in kept_sidecars:
                    path.unlink(missing_ok=True)
            for path in self.cache_dir.glob("*.lock"):
                if path == global_lock_path:
                    continue
                if path not in kept_sidecars:
                    path.unlink(missing_ok=True)

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, separators=(",", ":"))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _read_json(path: Path, max_bytes: int) -> Any:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError(f"cached JSON exceeds {max_bytes} byte limit")
        return json.loads(payload)

    @staticmethod
    def _atomic_bytes(path: Path, value: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_text(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> TextResult:
        if time.monotonic() >= self._next_prune:
            self._next_prune = time.monotonic() + 300
            self.prune()
        body_path, meta_path = self._text_paths(url)
        lock_path = body_path.with_suffix(".html.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        global_lock_path = self.cache_dir / ".cache.lock"
        with global_lock_path.open("a+") as global_lock:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_SH)
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                return self._get_text_locked(url, body_path, meta_path, headers, True)

    def _get_text_locked(
        self,
        url: str,
        body_path: Path,
        meta_path: Path,
        headers: dict[str, str] | None,
        allow_cache: bool,
    ) -> TextResult:
        request_headers = {"Accept": "text/html", "User-Agent": "job-scout/1.0"}
        request_headers.update(headers or {})
        if allow_cache and body_path.exists() and meta_path.exists():
            try:
                metadata = self._read_json(meta_path, 64 * 1024)
                if not isinstance(metadata, dict):
                    metadata = {}
            except (json.JSONDecodeError, OSError, ValueError):
                metadata = {}
            etag = metadata.get("etag")
            last_modified = metadata.get("last_modified")
            if isinstance(etag, str) and etag:
                request_headers["If-None-Match"] = etag
            if isinstance(last_modified, str) and last_modified:
                request_headers["If-Modified-Since"] = last_modified
        request = Request(url, headers=request_headers)
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    payload = response.read(self.max_response_bytes + 1)
                    if len(payload) > self.max_response_bytes:
                        raise ValueError(
                            f"response exceeds {self.max_response_bytes} byte limit"
                        )
                    content_type = response.headers.get_content_type()
                    if content_type != "text/html":
                        raise ValueError("response is not HTML")
                    text = payload.decode("utf-8", errors="strict")
                    self._atomic_bytes(body_path, payload)
                    self._atomic_json(
                        meta_path,
                        {
                            "etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified"),
                        },
                    )
                    return TextResult(text=text, from_cache=False, status=response.status)
            except HTTPError as error:
                if error.code == 304 and allow_cache and body_path.exists():
                    try:
                        payload = body_path.read_bytes()
                        if len(payload) > self.max_response_bytes:
                            raise ValueError("cached HTML exceeds response byte limit")
                        text = payload.decode("utf-8", errors="strict")
                    except (OSError, UnicodeDecodeError, ValueError):
                        body_path.unlink(missing_ok=True)
                        meta_path.unlink(missing_ok=True)
                        return self._get_text_locked(
                            url, body_path, meta_path, headers, False
                        )
                    return TextResult(text=text, from_cache=True, status=304)
                if error.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = min(float(retry_after), 30.0) if retry_after else 0.5 * 2**attempt
                except ValueError:
                    delay = 0.5 * 2**attempt
                self.sleeper(max(0.0, delay))
            except (URLError, TimeoutError):
                if attempt >= self.retries:
                    raise
                self.sleeper(0.5 * 2**attempt)
        raise RuntimeError("unreachable")

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> JsonResult:
        if time.monotonic() >= self._next_prune:
            self._next_prune = time.monotonic() + 300
            self.prune()
        body_path, meta_path = self._paths(url)
        lock_path = body_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        global_lock_path = self.cache_dir / ".cache.lock"
        with global_lock_path.open("a+") as global_lock:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_SH)
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                return self._get_locked(url, body_path, meta_path, headers)

    def _get_locked(
        self,
        url: str,
        body_path: Path,
        meta_path: Path,
        headers: dict[str, str] | None,
    ) -> JsonResult:
        request_headers = {"Accept": "application/json", "User-Agent": "job-scout/1.0"}
        request_headers.update(headers or {})
        if body_path.exists() and meta_path.exists():
            try:
                metadata = self._read_json(meta_path, 64 * 1024)
                if not isinstance(metadata, dict):
                    metadata = {}
            except (json.JSONDecodeError, OSError, ValueError):
                metadata = {}
            etag = metadata.get("etag")
            last_modified = metadata.get("last_modified")
            if isinstance(etag, str) and etag:
                request_headers["If-None-Match"] = etag
            if isinstance(last_modified, str) and last_modified:
                request_headers["If-Modified-Since"] = last_modified
        request = Request(url, headers=request_headers)
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    payload = response.read(self.max_response_bytes + 1)
                    if len(payload) > self.max_response_bytes:
                        raise ValueError(
                            f"response exceeds {self.max_response_bytes} byte limit"
                        )
                    data = json.loads(payload)
                    self._atomic_json(body_path, data)
                    self._atomic_json(
                        meta_path,
                        {
                            "etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified"),
                        },
                    )
                    return JsonResult(data=data, from_cache=False, status=response.status)
            except HTTPError as error:
                if error.code == 304 and body_path.exists():
                    try:
                        cached_data = self._read_json(body_path, self.max_response_bytes)
                    except (json.JSONDecodeError, OSError, ValueError):
                        body_path.unlink(missing_ok=True)
                        meta_path.unlink(missing_ok=True)
                        return self._get_locked(url, body_path, meta_path, headers)
                    return JsonResult(
                        data=cached_data,
                        from_cache=True,
                        status=304,
                    )
                if error.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = min(float(retry_after), 30.0) if retry_after else 0.5 * 2**attempt
                except ValueError:
                    delay = 0.5 * 2**attempt
                self.sleeper(max(0.0, delay))
            except (URLError, TimeoutError):
                if attempt >= self.retries:
                    raise
                self.sleeper(0.5 * 2**attempt)
        raise RuntimeError("unreachable")
