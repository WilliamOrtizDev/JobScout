import html
import re
from datetime import datetime, timezone
from functools import lru_cache
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

from .models import Candidate

_UNSAFE_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be string")
    return value


def clean_html(value: str | None) -> str:
    text = _UNSAFE_BLOCK.sub(" ", value or "")
    text = html.unescape(text)
    return _SPACE.sub(" ", _TAG.sub(" ", text)).strip()


@lru_cache(maxsize=1024)
def _hostname_resolves_public(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return bool(addresses) and all(
        ipaddress.ip_address(item[4][0]).is_global for item in addresses
    )


def public_https_url(value: Any) -> str:
    url = str(value or "")
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    try:
        explicit_port = parts.port
        port = explicit_port if explicit_port is not None else 443
    except ValueError:
        raise ValueError("unsafe public URL") from None
    unsafe = (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or not 1 <= port <= 65535
        or host == "localhost"
        or host.endswith(
            (".localhost", ".local", ".internal", ".test", ".invalid", ".example")
        )
    )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            address = None
    if address is not None and not address.is_global:
        unsafe = True
    if address is None and host and not _hostname_resolves_public(host, port):
        unsafe = True
    if unsafe:
        raise ValueError("unsafe public URL")
    return url


def normalize_greenhouse(
    raw: dict[str, Any], *, tenant: str, company: str
) -> Candidate:
    url = public_https_url(raw["absolute_url"])
    return Candidate(
        source="greenhouse",
        tenant=tenant,
        source_id=str(raw["id"]),
        company=company,
        title=_required_text(raw["title"], "title"),
        location=(raw.get("location") or {}).get("name"),
        description=clean_html(raw.get("content")),
        employment_type=None,
        remote=None,
        posted_at=raw.get("first_published") or raw.get("updated_at"),
        canonical_url=url,
        apply_url=url,
        raw=raw,
    )


def _epoch_ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _lever_remote(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("workplaceType must be string")
    normalized = re.sub(r"[^a-z]", "", value.strip().lower())
    if not normalized or normalized == "unspecified":
        return None
    if normalized == "remote":
        return True
    if normalized in {"onsite", "hybrid"}:
        return False
    raise ValueError("unknown workplaceType")


def normalize_lever(raw: dict[str, Any], *, tenant: str, company: str) -> Candidate:
    categories = raw.get("categories") or {}
    hosted_url = public_https_url(raw["hostedUrl"])
    apply_url = public_https_url(raw.get("applyUrl") or hosted_url)
    return Candidate(
        source="lever",
        tenant=tenant,
        source_id=str(raw["id"]),
        company=company,
        title=_required_text(raw["text"], "title"),
        location=categories.get("location"),
        description=clean_html(raw.get("descriptionPlain") or raw.get("description")),
        employment_type=categories.get("commitment"),
        remote=_lever_remote(raw.get("workplaceType")),
        posted_at=_epoch_ms_to_iso(raw.get("createdAt")),
        canonical_url=hosted_url,
        apply_url=apply_url,
        raw=raw,
    )


def normalize_ashby(raw: dict[str, Any], *, tenant: str, company: str) -> Candidate:
    if "isListed" not in raw:
        raise ValueError("missing isListed")
    if "isListed" in raw and not isinstance(raw["isListed"], bool):
        raise ValueError("isListed must be boolean")
    if raw.get("isListed") is False:
        raise ValueError("unlisted Ashby job")
    if "isRemote" in raw and raw["isRemote"] is not None and not isinstance(raw["isRemote"], bool):
        raise ValueError("isRemote must be boolean")
    workplace_type = re.sub(r"[^a-z]", "", str(raw.get("workplaceType") or "").lower())
    if isinstance(raw.get("isRemote"), bool):
        remote = raw["isRemote"]
    elif workplace_type == "remote":
        remote = True
    elif workplace_type in {"onsite", "hybrid"}:
        remote = False
    else:
        remote = None
    job_url = public_https_url(raw["jobUrl"])
    apply_url = public_https_url(raw.get("applyUrl") or job_url)
    return Candidate(
        source="ashby",
        tenant=tenant,
        source_id=str(raw.get("id") or job_url.rstrip("/").rsplit("/", 1)[-1]),
        company=company,
        title=_required_text(raw["title"], "title"),
        location=raw.get("location"),
        description=clean_html(raw.get("descriptionPlain") or raw.get("descriptionHtml")),
        employment_type=raw.get("employmentType"),
        remote=remote,
        posted_at=raw.get("publishedAt"),
        canonical_url=job_url,
        apply_url=apply_url,
        raw=raw,
    )


def normalize_smartrecruiters(
    raw: dict[str, Any], *, tenant: str, company: str
) -> Candidate:
    location = raw.get("location") or {}
    employment = raw.get("typeOfEmployment") or {}
    sections = (raw.get("jobAd") or {}).get("sections") or {}
    description = (sections.get("jobDescription") or {}).get("text")
    if "active" in raw and raw["active"] is not None and not isinstance(raw["active"], bool):
        raise ValueError("active must be boolean")
    if raw.get("active") is False:
        raise ValueError("inactive SmartRecruiters job")
    if not raw.get("applyUrl"):
        raise ValueError("missing application URL")
    apply_url = public_https_url(raw["applyUrl"])
    canonical_url = public_https_url(raw.get("postingUrl") or apply_url)
    location_text = location.get("fullLocation") or location.get("city")
    explicit_remote = location.get("remote")
    if explicit_remote is not None and not isinstance(explicit_remote, bool):
        raise ValueError("location.remote must be boolean")
    if isinstance(explicit_remote, bool):
        remote = explicit_remote
    else:
        remote = True if "remote" in str(location_text).lower() else None
    return Candidate(
        source="smartrecruiters",
        tenant=tenant,
        source_id=str(raw["id"]),
        company=company,
        title=_required_text(raw.get("name") or raw.get("title"), "title"),
        location=location_text,
        description=clean_html(description),
        employment_type=employment.get("label") or employment.get("id"),
        remote=remote,
        posted_at=raw.get("releasedDate"),
        canonical_url=canonical_url,
        apply_url=apply_url,
        raw=raw,
    )
