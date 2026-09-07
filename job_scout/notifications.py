from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable


def _text(value: Any, fallback: str = "Not stated") -> str:
    if value is None:
        return fallback
    collapsed = " ".join(str(value).split())
    collapsed = collapsed.replace("[[HERMES_MESSAGE_BREAK]]", "[HERMES_MESSAGE_BREAK]")
    collapsed = re.sub(r"(?i)\bMEDIA\s*:", "MEDIA :", collapsed)
    return collapsed or fallback


def _list_text(value: Any, fallback: str = "None noted") -> str:
    if not isinstance(value, list):
        return _text(value, fallback)
    items = [_text(item, "") for item in value]
    return "; ".join(item for item in items if item) or fallback


def _date_rank(value: Any) -> float:
    text = _text(value, "")
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _compensation_rank(value: Any) -> float:
    text = _text(value, "").lower().replace(",", "")
    amounts = [
        float(number) * (1000 if suffix else 1)
        for number, suffix in re.findall(r"(\d+(?:\.\d+)?)\s*(k)?", text)
    ]
    if not amounts:
        return 0.0
    amount = max(amounts)
    if "year" in text or "annual" in text or amount >= 1000:
        return amount / 2080
    return amount


def order_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order approval-ready jobs by fit, freshness, then compensation."""
    return sorted(
        jobs,
        key=lambda job: (
            float(job.get("fit_score") or 0),
            _date_rank(job.get("posted_date")),
            _compensation_rank(job.get("compensation")),
            _text(job.get("job_id"), ""),
        ),
        reverse=True,
    )


def load_packet(
    packet_dir: Path,
    *,
    root: Path | None = None,
    snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    packet = Path(packet_dir).resolve()
    allowed_root = Path(root).resolve() if root is not None else None
    if allowed_root is not None and not packet.is_relative_to(allowed_root):
        raise ValueError(f"packet is outside allowed root: {packet}")

    def packet_file(name: str) -> Path:
        path = (packet / name).resolve()
        if allowed_root is not None and not path.is_relative_to(allowed_root):
            raise ValueError(f"packet file is outside allowed root: {path}")
        return path

    job_path = packet_file("job.json")
    if snapshot is None and not job_path.is_file():
        raise ValueError(f"missing packet metadata: {job_path}")
    job_data = snapshot.get("job.json") if snapshot is not None else job_path.read_bytes()
    if job_data is None:
        raise ValueError(f"missing packet metadata: {job_path}")
    job = json.loads(job_data)
    for name in ("resume.pdf", "cover-letter.pdf"):
        document = packet_file(name)
        document_data = snapshot.get(name) if snapshot is not None else None
        if snapshot is not None:
            valid_document = bool(document_data)
        else:
            valid_document = document.is_file() and document.stat().st_size > 0
        if not valid_document:
            raise ValueError(f"missing supporting document: {document}")
        job[f"_{name.replace('-', '_').replace('.', '_')}"] = str(document)
    job["_packet_dir"] = str(packet)
    return job


def render_plain_messages(jobs: Iterable[dict[str, Any]]) -> list[str]:
    """Render ordered jobs as separate readable iMessage payloads."""
    ordered = order_jobs(jobs)
    messages: list[str] = []
    for job in ordered:
        packet = Path(str(job["_packet_dir"]))
        job_id = _text(job.get("job_id"), packet.name)
        packet_revision = _text(job.get("_packet_fingerprint"), "")[:12]
        fit_evidence = job.get("fit_evidence")
        evidence_lines = (
            [_text(item, "") for item in fit_evidence]
            if isinstance(fit_evidence, list)
            else [_text(fit_evidence, "None noted")]
        )
        caveats = job.get("caveats")
        caveat_lines = (
            [_text(item, "") for item in caveats]
            if isinstance(caveats, list)
            else [_text(caveats, "None noted")]
        )
        message = "\n".join(
            (
                _text(job.get("title")),
                _text(job.get("company")),
                "",
                "Fit:",
                f"{_text(job.get('fit_score'), '0')}/100",
                "",
                "Details:",
                f"Contract: {_text(job.get('engagement_type'))}",
                f"Location: {_text(job.get('remote_region'))}",
                f"Compensation: {_text(job.get('compensation'))}",
                "",
                "Why it matches:",
                *(line for line in evidence_lines if line),
                "",
                "Cautions:",
                *(line for line in caveat_lines if line),
                "",
                "Application:",
                _text(job.get("application_url")),
                f"Source: {_text(job.get('source_type'))}",
                "",
                "Approval:",
                f"APPROVE {job_id}",
                *((f"Packet revision: {packet_revision}",) if packet_revision else ()),
                "",
                f"MEDIA:{job['_resume_pdf']}",
                f"MEDIA:{job['_cover_letter_pdf']}",
            )
        )
        messages.append(message)
    return messages


def render_plain_text(jobs: Iterable[dict[str, Any]]) -> str:
    """Render ordered jobs as one stream for legacy auto-delivery."""
    messages = render_plain_messages(jobs)
    return "\n[[HERMES_MESSAGE_BREAK]]\n".join(messages) if messages else "[SILENT]"
