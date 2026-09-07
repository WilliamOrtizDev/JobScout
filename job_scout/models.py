from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url_key(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.lower().startswith("utm_")
        and name.lower() not in {"gh_src", "source", "lever-source"}
    ]
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    host_text = f"[{host}]" if ":" in host else host
    port = parts.port
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host_text if port is None or default_port else f"{host_text}:{port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


@dataclass(frozen=True)
class Candidate:
    source: str
    tenant: str
    source_id: str
    company: str
    title: str
    location: str | None
    description: str
    employment_type: str | None
    remote: bool | None
    posted_at: str | None
    canonical_url: str
    apply_url: str
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "source",
            "tenant",
            "source_id",
            "company",
            "title",
            "description",
            "canonical_url",
            "apply_url",
        ):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be string")
        if self.remote is not None and not isinstance(self.remote, bool):
            raise ValueError("remote must be boolean or null")
        for name in ("location", "employment_type", "posted_at"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be string or null")

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        value = asdict(self)
        raw = value.pop("raw")
        fingerprint_value = dict(value)
        fingerprint_value["canonical_url"] = canonical_url_key(value["canonical_url"])
        fingerprint_value["apply_url"] = canonical_url_key(value["apply_url"])
        fingerprint = json.dumps(
            fingerprint_value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        value["content_hash"] = sha256(fingerprint).hexdigest()
        if include_raw:
            value["raw"] = raw
        return value
