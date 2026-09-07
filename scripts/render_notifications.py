#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.notifications import load_packet, render_plain_messages, render_plain_text
from job_scout.state_store import (
    SQLiteStateStore,
    materialize_delivery_snapshot,
    packet_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render an ordered plain-text Photon/iMessage job batch"
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Allowed packet root (defaults to <database-parent>/applications)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit ordered per-job message payloads as JSON",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Read pending packet notifications from the SQLite outbox",
    )
    parser.add_argument("packet_dirs", nargs="*", type=Path)
    args = parser.parse_args()
    applications_root = args.root or (
        args.database.parent / "applications"
        if args.database is not None
        else Path.home() / "job-search/applications"
    )
    packet_dirs = list(args.packet_dirs)
    store = None
    claim_token: str | None = None
    claimed_items: list[dict] = []
    if args.database is not None:
        if packet_dirs:
            parser.error("packet directories cannot be combined with --database")
        store = SQLiteStateStore(args.database, applications_root=applications_root)
        claim = store.claim_outbox()
        claim_token = claim["claim_token"]
        claimed_items = claim["items"]
        if not claimed_items:
            print("[SILENT]")
            return 0
    elif not packet_dirs:
        parser.error("provide packet directories or --database")

    try:
        jobs = []
        for item in claimed_items:
            payload = item["payload"]
            packet_dir = Path(str(payload.get("packet_dir") or ""))
            expected_fingerprint = item.get("packet_fingerprint")
            expected_url = item.get("application_url")
            if payload.get("packet_fingerprint") != expected_fingerprint:
                raise ValueError("outbox packet fingerprint payload mismatch")
            if payload.get("application_url") != expected_url:
                raise ValueError("outbox application URL payload mismatch")
            actual_fingerprint, snapshot = packet_snapshot(
                packet_dir, applications_root=applications_root
            )
            if actual_fingerprint != expected_fingerprint:
                raise ValueError("packet changed after notification was queued")
            job = load_packet(packet_dir, root=applications_root, snapshot=snapshot)
            delivery_paths = materialize_delivery_snapshot(
                snapshot,
                packet_fingerprint=actual_fingerprint,
                applications_root=applications_root,
            )
            job["_resume_pdf"] = str(delivery_paths["resume.pdf"])
            job["_cover_letter_pdf"] = str(delivery_paths["cover-letter.pdf"])
            if job.get("application_url") != expected_url:
                raise ValueError("job.json application URL does not match outbox")
            job["job_id"] = item["job_id"]
            job["application_url"] = expected_url
            job["_packet_fingerprint"] = item["packet_fingerprint"]
            jobs.append(job)
        if not claimed_items:
            jobs = [load_packet(path, root=applications_root) for path in packet_dirs]
        rendered = (
            json.dumps({"messages": render_plain_messages(jobs)})
            if args.json
            else render_plain_text(jobs)
        )
        sys.stdout.write(f"{rendered}\n")
        sys.stdout.flush()
        if store is not None and claim_token is not None:
            marked = store.mark_outbox_claim_dispatched(claim_token)
            if marked != len(claimed_items):
                raise RuntimeError("outbox claim changed before dispatch bookkeeping")
    except Exception as exc:
        if store is not None and claim_token is not None:
            store.release_outbox_claim(claim_token, str(exc))
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
