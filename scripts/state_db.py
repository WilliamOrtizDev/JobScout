#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.state_store import SQLiteStateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the job scout SQLite state")
    parser.add_argument(
        "--database", type=Path, default=ROOT / "runtime/job-scout.db"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Import or refresh legacy JSON state")
    importer.add_argument("state", type=Path)
    exporter = commands.add_parser("export", help="Export a JSON rollback snapshot")
    exporter.add_argument("output", type=Path)
    commands.add_parser("status", help="Print schema and row counts")
    recorder = commands.add_parser("record", help="Record a verified lifecycle decision")
    recorder.add_argument("job_id")
    recorder.add_argument("--record", type=Path, required=True)
    recorder.add_argument("--reason", required=True)
    recorder.add_argument("--notify", action="store_true")
    recorder.add_argument(
        "--verification",
        type=Path,
        help="Verification object to commit in the same transaction",
    )
    shower = commands.add_parser("show", help="Show one application record")
    shower.add_argument("job_id")
    listing = commands.add_parser("list", help="List application records")
    listing.add_argument("--status")
    outbox = commands.add_parser("outbox", help="List delivery outbox records")
    outbox.add_argument("--status", default="pending")
    delivered = commands.add_parser("outbox-delivered", help="Record confirmed delivery")
    delivered.add_argument("outbox_id", type=int)
    failed = commands.add_parser("outbox-failed", help="Record a failed delivery")
    failed.add_argument("outbox_id", type=int)
    failed.add_argument("--error", required=True)
    retry = commands.add_parser("outbox-retry", help="Retry a failed delivery")
    retry.add_argument("outbox_id", type=int)
    verifier = commands.add_parser("verify", help="Record live verification evidence")
    verifier.add_argument("job_id")
    verifier.add_argument("--outcome", required=True)
    verifier.add_argument("--source-url")
    verifier.add_argument("--evidence", type=Path, required=True)
    approver = commands.add_parser("approve", help="Bind approval to the exact packet and URL")
    approver.add_argument("job_id")
    submitter = commands.add_parser("submit", help="Record a verified submission outcome")
    submitter.add_argument("job_id")
    submitter.add_argument("--outcome", choices=("submitted", "blocked"), required=True)
    submitter.add_argument("--confirmation", type=Path)
    submitter.add_argument("--error")
    args = parser.parse_args()

    store = SQLiteStateStore(args.database)
    if args.command == "import":
        print(json.dumps({"database": str(args.database), "imported": store.import_legacy_json(args.state)}, sort_keys=True))
    elif args.command == "export":
        print(json.dumps({"database": str(args.database), "exported": store.export_legacy_json(args.output), "output": str(args.output)}, sort_keys=True))
    elif args.command == "record":
        record = json.loads(args.record.read_text())
        verification = (
            json.loads(args.verification.read_text())
            if args.verification is not None
            else None
        )
        store.record_application_decision(
            args.job_id,
            record,
            reason=args.reason,
            notify=args.notify,
            verification=verification,
        )
        print(json.dumps({"database": str(args.database), "job_id": args.job_id, "recorded": True}, sort_keys=True))
    elif args.command == "show":
        record = store.get_application_record(args.job_id)
        if record is None:
            parser.error(f"unknown job ID: {args.job_id}")
        print(json.dumps({"job_id": args.job_id, **record}, ensure_ascii=False, sort_keys=True))
    elif args.command == "list":
        print(json.dumps({"items": store.list_application_records(args.status)}, ensure_ascii=False, sort_keys=True))
    elif args.command == "outbox":
        print(json.dumps({"items": store.list_outbox(args.status)}, ensure_ascii=False, sort_keys=True))
    elif args.command == "outbox-delivered":
        print(json.dumps({"outbox_id": args.outbox_id, "updated": store.mark_outbox_delivered(args.outbox_id)}, sort_keys=True))
    elif args.command == "outbox-failed":
        print(json.dumps({"outbox_id": args.outbox_id, "updated": store.mark_outbox_failed(args.outbox_id, args.error)}, sort_keys=True))
    elif args.command == "outbox-retry":
        print(json.dumps({"outbox_id": args.outbox_id, "updated": store.retry_outbox(args.outbox_id)}, sort_keys=True))
    elif args.command == "verify":
        verification_id = store.record_verification(
            args.job_id,
            outcome=args.outcome,
            source_url=args.source_url,
            evidence=json.loads(args.evidence.read_text()),
        )
        print(json.dumps({"job_id": args.job_id, "verification_id": verification_id}, sort_keys=True))
    elif args.command == "approve":
        print(json.dumps({"job_id": args.job_id, "approval_id": store.approve_application(args.job_id)}, sort_keys=True))
    elif args.command == "submit":
        confirmation = (
            json.loads(args.confirmation.read_text()) if args.confirmation is not None else None
        )
        submission_id = store.record_submission(
            args.job_id,
            outcome=args.outcome,
            confirmation=confirmation,
            error=args.error,
        )
        print(json.dumps({"job_id": args.job_id, "submission_id": submission_id}, sort_keys=True))
    else:
        print(json.dumps({"database": str(args.database), **store.stats()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
