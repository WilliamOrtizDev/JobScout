#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

HOME = Path.home()
AUTOMATION = Path(os.environ.get("JOB_SEARCH_AUTOMATION", HOME / "JobScout"))
JOB_SEARCH = Path(os.environ.get("JOB_SEARCH_WORKDIR", HOME / "job-search"))
sys.path.insert(0, str(AUTOMATION))

from job_scout.community_client import community_python_executable
from job_scout.queueing import rotating_batch
from job_scout.state_store import SQLiteStateStore
OUTPUT = AUTOMATION / "runtime/live-scout-input.json"
DATABASE = JOB_SEARCH / "job-scout.db"
PROFILE = JOB_SEARCH / "search-profile.json"
collector_python = community_python_executable(AUTOMATION, os.environ)
command = [
    collector_python,
    str(AUTOMATION / "scripts/collect.py"),
    "--profile",
    str(PROFILE),
    "--database",
    str(DATABASE),
    "--output",
    str(OUTPUT),
    "--cache",
    str(AUTOMATION / "runtime/http-cache"),
]
completed = subprocess.run(
    command,
    cwd=AUTOMATION,
    check=False,
    capture_output=True,
    text=True,
    timeout=90,
)
if completed.returncode != 0:
    print(json.dumps({"collector_error": (completed.stderr or completed.stdout)[-2000:]}))
    raise SystemExit(completed.returncode)

payload = json.loads(OUTPUT.read_text())
leads = []
store = SQLiteStateStore(DATABASE)
queued = rotating_batch(
    payload.get("jobs", []),
    limit=25,
    cursor_store=store,
    queue_name="live-scout",
)
for job in queued:
    lead = dict(job)
    lead["description"] = lead.get("description", "")[:3000]
    leads.append(lead)
print(
    json.dumps(
        {
            "deterministic_shadow_collector": {
                "counts": payload.get("counts", {}),
                "source_failures": payload.get("source_failures", []),
                "new_or_changed_leads": leads,
                "instruction": "Treat these as unverified leads. Verify live status, remote/contract/compensation, and the exact application route before packet generation.",
            }
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
