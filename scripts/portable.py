#!/usr/bin/env python3
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.portability import RuntimePaths, backup_runtime, doctor, restore_runtime


def runtime_paths() -> RuntimePaths:
    return RuntimePaths.from_environment(
        ROOT,
        home=Path.home(),
        environment=os.environ,
    )


def source_commit() -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def doctor_command(as_json: bool) -> int:
    checks = doctor(runtime_paths())
    ready = all(check.ok for check in checks if check.required)
    if as_json:
        print(
            json.dumps(
                {"ready": ready, "checks": [asdict(check) for check in checks]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for check in checks:
            status = "PASS" if check.ok else ("FAIL" if check.required else "WARN")
            print(f"{status:<4}  {check.name:<26} {check.detail}")
        print("READY" if ready else "NOT READY")
    return 0 if ready else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Portable setup diagnostics and state migration for JobScout"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="check runtime readiness")
    doctor_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    backup_parser = subparsers.add_parser("backup", help="create a consistent private runtime backup")
    backup_parser.add_argument("archive", type=Path)
    restore_parser = subparsers.add_parser("restore", help="restore into empty configured destinations")
    restore_parser.add_argument("archive", type=Path)
    args = parser.parse_args()

    if args.command == "doctor":
        return doctor_command(args.json)
    if args.command == "backup":
        manifest = backup_runtime(
            runtime_paths(), args.archive, source_commit=source_commit()
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if args.command == "restore":
        manifest = restore_runtime(args.archive, runtime_paths())
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
