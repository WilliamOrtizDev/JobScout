from datetime import datetime
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit
import urllib.request

from .community_sources import _parse_timestamp, select_openroles_chunks
from .sources import public_https_url

_ATS_MANIFEST_URL = "https://storage.stapply.ai/jobhive/v1/manifest.json"
_OPENROLES_MANIFEST_URL = "https://openroles.today/data/manifest.json"
_ATS_NAME = re.compile(r"[a-z0-9_]+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

JsonFetch = Callable[[str, int], Any]
BinaryFetch = Callable[[str, int], bytes]
ParquetQuery = Callable[
    [str, datetime, list[str], int, bool, list[str]], list[dict[str, Any]]
]


def community_python_executable(
    automation_root: Path,
    environment: Mapping[str, str],
    *,
    path_exists: Callable[[Path], bool] = Path.exists,
) -> str:
    override = environment.get("JOB_SCOUT_COLLECTOR_PYTHON")
    if override:
        return override
    venv = Path(
        environment.get("JOB_SCOUT_COMMUNITY_VENV", str(automation_root / ".venv-community"))
    )
    executable = venv / "bin/python"
    return str(executable) if path_exists(executable) else "python3"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin_url: str) -> None:
        self.origin = _url_origin(public_https_url(origin_url))
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _url_origin(newurl) != self.origin:
            raise ValueError("cross-origin redirect rejected")
        public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _url_origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."), parts.port or 443)


def _openroles_row_is_recent(row: dict[str, Any], cutoff: datetime) -> bool:
    try:
        return _parse_timestamp(row.get("p"), "posted timestamp") >= cutoff
    except ValueError:
        return False


def _default_json_fetch(url: str, max_bytes: int) -> Any:
    body = _fetch_bytes(url, max_bytes, "application/json")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON response") from error


def _default_binary_fetch(url: str, max_bytes: int) -> bytes:
    return _fetch_bytes(url, max_bytes, "application/gzip, application/octet-stream")


def _fetch_bytes(url: str, max_bytes: int, accept: str) -> bytes:
    safe_url = public_https_url(url)
    request = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "job-scout-community-sources/1.0",
            "Accept": accept,
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler(safe_url))
    with opener.open(request, timeout=30) as response:
        final_url = public_https_url(response.geturl())
        if urlsplit(final_url).hostname != urlsplit(safe_url).hostname:
            raise ValueError("cross-host redirect rejected")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > max_bytes:
            raise ValueError("response exceeds byte limit")
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("response exceeds byte limit")
    return body


def query_ats_parquet(
    dataset_url: str,
    cutoff: datetime,
    titles: list[str],
    max_rows: int,
    require_remote: bool,
    country_codes: list[str],
) -> list[dict[str, Any]]:
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "duckdb is required; run scripts/install_community_sources.sh"
        ) from error

    if not 1 <= max_rows <= 20_000:
        raise ValueError("max_rows must be between 1 and 20000")
    dataset_path = Path(dataset_url).resolve()
    if not dataset_path.is_file() or dataset_path.suffix != ".parquet":
        raise ValueError("dataset must be a local parquet file")
    path_text = str(dataset_path)
    if "'" in path_text:
        raise ValueError("unsafe parquet path")
    title_predicates = " OR ".join("lower(title) LIKE ?" for _ in titles)
    if not title_predicates:
        raise ValueError("at least one title is required")
    columns = [
        "url",
        "apply_url",
        "title",
        "company",
        "ats_type",
        "ats_id",
        "requisition_id",
        "location",
        "is_remote",
        "employment_type",
        "commitment",
        "description",
        "posted_at",
    ]
    extra_predicates: list[str] = []
    extra_parameters: list[Any] = []
    if require_remote:
        extra_predicates.append(
            "(is_remote IS TRUE OR lower(coalesce(location, '')) LIKE '%remote%')"
        )
    if country_codes:
        placeholders = ", ".join("?" for _ in country_codes)
        extra_predicates.append(f"upper(country_iso) IN ({placeholders})")
        extra_parameters.extend(country_codes)
    extra_where = "".join(f" AND {predicate}" for predicate in extra_predicates)
    query = f"""
        SELECT {', '.join(columns)}
        FROM read_parquet('{path_text}')
        WHERE try_cast(posted_at AS TIMESTAMPTZ) >= ?
          AND ({title_predicates})
          {extra_where}
        ORDER BY try_cast(posted_at AS TIMESTAMPTZ) DESC NULLS LAST
        LIMIT ?
    """
    parameters: list[Any] = [cutoff]
    parameters.extend(f"%{title.strip().lower()}%" for title in titles)
    parameters.extend(extra_parameters)
    parameters.append(max_rows)
    connection = duckdb.connect()
    try:
        connection.execute("SET autoinstall_known_extensions = false")
        connection.execute("SET autoload_known_extensions = false")
        connection.execute("SET allow_community_extensions = false")
        connection.execute("SET allowed_paths = ?", [[path_text]])
        connection.execute("SET enable_external_access = false")
        connection.execute("SET enable_progress_bar = false")
        cursor = connection.execute(query, parameters)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


class CommunityDatasetClient:
    def __init__(
        self,
        *,
        cache_dir: str | Path,
        json_fetch: JsonFetch = _default_json_fetch,
        binary_fetch: BinaryFetch = _default_binary_fetch,
        parquet_query: ParquetQuery = query_ats_parquet,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.json_fetch = json_fetch
        self.binary_fetch = binary_fetch
        self.parquet_query = parquet_query

    @staticmethod
    def _same_origin_url(manifest_url: str, reference: str) -> str:
        base = public_https_url(manifest_url)
        joined = urljoin(base, reference)
        base_parts = urlsplit(base)
        target_parts = urlsplit(joined)
        if (base_parts.scheme, base_parts.hostname, base_parts.port) != (
            target_parts.scheme,
            target_parts.hostname,
            target_parts.port,
        ):
            raise ValueError("dataset reference must remain on the manifest same origin")
        return public_https_url(joined)

    def _ats_manifest(self, manifest_url: str, cache_seconds: int) -> dict[str, Any]:
        if not isinstance(cache_seconds, int) or not 0 <= cache_seconds <= 86_400:
            raise ValueError("manifest_cache_seconds must be between 0 and 86400")
        cache_path = self.cache_dir / "ats-scrapers-manifest.json"
        lock_path = self.cache_dir / "ats-scrapers-manifest.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if cache_seconds and cache_path.exists():
                age = time.time() - cache_path.stat().st_mtime
                if 0 <= age <= cache_seconds:
                    try:
                        cached = json.loads(cache_path.read_text(encoding="utf-8"))
                        if isinstance(cached, dict):
                            return cached
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        cache_path.unlink(missing_ok=True)
            fetched = self.json_fetch(manifest_url, 2 * 1024 * 1024)
            if not isinstance(fetched, dict):
                raise ValueError("ats-scrapers manifest must be an object")
            encoded = json.dumps(fetched, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError("ats-scrapers manifest exceeds byte limit")
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.cache_dir,
                    prefix=".ats-scrapers-manifest.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                Path(temporary_name).replace(cache_path)
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
            return fetched

    def fetch_ats_scrapers(
        self,
        entry: dict[str, Any],
        *,
        cutoff: datetime,
        titles: list[str],
    ) -> list[dict[str, Any]]:
        manifest_url = public_https_url(str(entry.get("manifest_url") or ""))
        if manifest_url != _ATS_MANIFEST_URL:
            raise ValueError("ats-scrapers manifest URL is not pinned")
        manifest = self._ats_manifest(
            manifest_url, entry.get("manifest_cache_seconds", 21_600)
        )
        if not isinstance(manifest, dict) or manifest.get("version") != "2.0":
            raise ValueError("unsupported ats-scrapers manifest schema")
        by_ats = manifest.get("by_ats")
        if not isinstance(by_ats, dict):
            raise ValueError("ats-scrapers manifest is missing by_ats partitions")
        max_rows = entry.get("max_rows", 5000)
        if not isinstance(max_rows, int):
            raise ValueError("max_rows must be an integer")
        require_remote = entry.get("require_remote", False)
        country_codes = entry.get("country_codes", [])
        if not isinstance(require_remote, bool):
            raise ValueError("require_remote must be a boolean")
        if not isinstance(country_codes, list) or any(
            not isinstance(code, str) or len(code.strip()) != 2
            for code in country_codes
        ):
            raise ValueError("country_codes must contain two-letter strings")
        ats_types = entry.get("ats_types", [])
        max_files = entry.get("max_files", 24)
        max_dataset_bytes = entry.get("max_dataset_bytes", 160 * 1024 * 1024)
        if (
            not isinstance(ats_types, list)
            or not ats_types
            or any(
                not isinstance(name, str) or _ATS_NAME.fullmatch(name) is None
                for name in ats_types
            )
        ):
            raise ValueError("ats_types must be a non-empty list of safe names")
        if not isinstance(max_files, int) or not 1 <= max_files <= 32:
            raise ValueError("max_files must be between 1 and 32")
        if len(ats_types) > max_files:
            raise ValueError("ats-scrapers partitions exceed file budget")
        if not isinstance(max_dataset_bytes, int) or not 1 <= max_dataset_bytes <= 256 * 1024 * 1024:
            raise ValueError("invalid ats-scrapers dataset byte budget")

        selected: list[tuple[str, str, int, str]] = []
        total_bytes = 0
        for ats_type in ats_types:
            metadata = by_ats.get(ats_type)
            if not isinstance(metadata, dict):
                raise ValueError(f"ats-scrapers partition missing: {ats_type}")
            reference = metadata.get("parquet")
            digest = metadata.get("parquet_sha256")
            size = metadata.get("parquet_size_bytes")
            if (
                not isinstance(reference, str)
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or not isinstance(size, int)
                or size < 1
                or size > 64 * 1024 * 1024
            ):
                raise ValueError(f"invalid ats-scrapers partition metadata: {ats_type}")
            url = self._same_origin_url(manifest_url, reference)
            expected = f"https://storage.stapply.ai/jobhive/v1/{ats_type}/jobs.parquet"
            if url != expected:
                raise ValueError("ats-scrapers partition URL is not pinned")
            total_bytes += size
            if total_bytes > max_dataset_bytes:
                raise ValueError("ats-scrapers partitions exceed dataset byte budget")
            selected.append((ats_type, url, size, digest))

        normalized_countries = [code.strip().upper() for code in country_codes]
        rows: list[dict[str, Any]] = []
        for ats_type, url, size, digest in selected:
            path = self._ats_partition_path(ats_type, url, size, digest)
            remaining = max_rows - len(rows)
            if remaining <= 0:
                break
            rows.extend(
                self.parquet_query(
                    str(path),
                    cutoff,
                    titles,
                    remaining,
                    require_remote,
                    normalized_countries,
                )
            )
        return rows

    def _ats_partition_path(
        self, ats_type: str, url: str, size: int, digest: str
    ) -> Path:
        cache_path = self.cache_dir / f"ats-{ats_type}-{digest}.parquet"
        lock_path = self.cache_dir / f"ats-{ats_type}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if cache_path.exists():
                body = cache_path.read_bytes()
                if len(body) == size and hashlib.sha256(body).hexdigest() == digest:
                    return cache_path
                cache_path.unlink(missing_ok=True)
            body = self.binary_fetch(url, size)
            if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                raise ValueError(f"ats-scrapers partition integrity failed: {ats_type}")
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.cache_dir,
                    prefix=f".{cache_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    temporary.write(body)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                Path(temporary_name).replace(cache_path)
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
            return cache_path

    @staticmethod
    def _verify_openroles_chunk(body: bytes, chunk: dict[str, Any]) -> list[Any]:
        if len(body) != int(chunk["bytes_gz"]):
            raise ValueError("openroles chunk compressed size mismatch")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
                raw = archive.read(int(chunk["bytes_raw"]) + 1)
        except (OSError, EOFError) as error:
            raise ValueError("invalid openroles gzip chunk") from error
        if len(raw) != int(chunk["bytes_raw"]):
            raise ValueError("openroles chunk raw size mismatch")
        if hashlib.sha256(raw).hexdigest()[:16] != chunk["sha"]:
            raise ValueError("openroles chunk digest mismatch")
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid openroles chunk JSON") from error
        if not isinstance(parsed, list) or len(parsed) != int(chunk["rows"]):
            raise ValueError("openroles chunk row count mismatch")
        return parsed

    def _chunk_rows(self, chunk_url: str, chunk: dict[str, Any]) -> list[Any]:
        digest = str(chunk["sha"])
        max_bytes = int(chunk["bytes_gz"])
        cache_path = self.cache_dir / f"openroles-{digest}.json.gz"
        lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if cache_path.exists():
                try:
                    return self._verify_openroles_chunk(cache_path.read_bytes(), chunk)
                except ValueError:
                    cache_path.unlink(missing_ok=True)
            body = self.binary_fetch(chunk_url, max_bytes)
            parsed = self._verify_openroles_chunk(body, chunk)
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.cache_dir,
                    prefix=f".{cache_path.name}.", suffix=".tmp", delete=False
                ) as temporary:
                    temporary_name = temporary.name
                    temporary.write(body)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                Path(temporary_name).replace(cache_path)
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
            return parsed

    def fetch_openroles(
        self,
        entry: dict[str, Any],
        *,
        cutoff: datetime,
        titles: list[str],
    ) -> list[dict[str, Any]]:
        manifest_url = public_https_url(str(entry.get("manifest_url") or ""))
        if manifest_url != _OPENROLES_MANIFEST_URL:
            raise ValueError("openroles manifest URL is not pinned")
        manifest = self.json_fetch(manifest_url, 4 * 1024 * 1024)
        if not isinstance(manifest, dict):
            raise ValueError("openroles manifest must be an object")
        max_chunks = entry.get("max_chunks", 16)
        byte_budget = entry.get("max_compressed_bytes", 32 * 1024 * 1024)
        raw_byte_budget = entry.get("max_raw_bytes", 128 * 1024 * 1024)
        candidate_row_budget = entry.get("max_candidate_rows", 320_000)
        max_candidates = entry.get("max_candidates", 5_000)
        if any(
            not isinstance(value, int)
            for value in (
                max_chunks,
                byte_budget,
                raw_byte_budget,
                candidate_row_budget,
                max_candidates,
            )
        ):
            raise ValueError("openroles budgets must be integers")
        if not 1 <= max_candidates <= 20_000:
            raise ValueError("max_candidates must be between 1 and 20000")
        chunks = select_openroles_chunks(
            manifest,
            cutoff=cutoff,
            max_chunks=max_chunks,
            max_compressed_bytes=byte_budget,
            max_raw_bytes=raw_byte_budget,
            max_candidate_rows=candidate_row_budget,
        )
        require_remote = entry.get("require_remote", False)
        country_codes = entry.get("country_codes", [])
        if not isinstance(require_remote, bool):
            raise ValueError("require_remote must be a boolean")
        if not isinstance(country_codes, list) or any(
            not isinstance(code, str) or len(code.strip()) != 2
            for code in country_codes
        ):
            raise ValueError("country_codes must contain two-letter strings")
        allowed_countries = {code.strip().upper() for code in country_codes}

        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_url = self._same_origin_url(manifest_url, str(chunk["file"]))
            parsed = self._chunk_rows(chunk_url, chunk)
            lowered_titles = tuple(title.strip().lower() for title in titles)
            matching = [
                row
                for row in parsed
                if isinstance(row, dict)
                and isinstance(row.get("ti"), str)
                and any(term in row["ti"].lower() for term in lowered_titles)
                and _openroles_row_is_recent(row, cutoff)
                and (not require_remote or str(row.get("w") or "").lower() == "remote")
                and (
                    not allowed_countries
                    or str(row.get("cc") or "").upper() in allowed_countries
                )
            ]
            if len(rows) + len(matching) > max_candidates:
                raise ValueError("openroles results exceed candidate budget")
            rows.extend(matching)
        return rows
