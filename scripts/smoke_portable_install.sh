#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT
mkdir -p "$temporary/bin" "$temporary/jobs" "$temporary/private"
printf '#!/usr/bin/env sh\nexit 0\n' > "$temporary/bin/hermes"
printf '#!/usr/bin/env sh\nexit 0\n' > "$temporary/bin/tectonic"
chmod 755 "$temporary/bin/hermes" "$temporary/bin/tectonic"
python3 - "$ROOT" "$temporary/jobs" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
workspace = Path(sys.argv[2])
search = json.loads((root / "config/search-profile.example.json").read_text())
search["candidate"]["name"] = "Portable Candidate"
(workspace / "search-profile.json").write_text(json.dumps(search))
application = json.loads((root / "config/application-profile.example.json").read_text())
application["status"] = "configured"
(workspace / "application-profile.json").write_text(json.dumps(application))
PY
printf '\\documentclass{article}\\begin{document}Portable\\end{document}\n' > "$temporary/private/resume.tex"
export HOME="$temporary"
export PATH="$temporary/bin:$PATH"
export HERMES_HOME="$temporary/hermes"
export JOB_SEARCH_AUTOMATION="$ROOT"
export JOB_SEARCH_WORKDIR="$temporary/jobs"
export RESUME_DIR="$temporary/resume"
export MASTER_RESUME_FILE="$temporary/private/custom-master.tex"
export PRIVATE_RESUME_FILE="$temporary/private/resume.tex"
export TECTONIC="$temporary/bin/tectonic"
"$ROOT/scripts/install.sh"
test -f "$MASTER_RESUME_FILE"
test ! -e "$RESUME_DIR/master-resume.tex"
python3 "$ROOT/scripts/portable.py" doctor --json > "$temporary/doctor.json"
python3 - "$temporary/doctor.json" <<'PY'
import json
import sys
result = json.load(open(sys.argv[1]))
if not result["ready"]:
    raise SystemExit("portable install doctor did not report ready")
PY
python3 - "$HERMES_HOME/scripts/job-scout-shadow.py" "$ROOT" "$HERMES_HOME/skills/productivity/continuous-job-search/SKILL.md" "$JOB_SEARCH_WORKDIR/cron-prompt.md" <<'PY'
from pathlib import Path
import re
import sys
launcher = Path(sys.argv[1]).read_text()
if str(Path(sys.argv[2]).resolve()) not in launcher:
    raise SystemExit("launcher does not contain the rendered repository path")
for raw in sys.argv[3:]:
    text = Path(raw).read_text()
    if re.search(r"\{\{[A-Z_][A-Z0-9_]*\}\}", text):
        raise SystemExit(f"rendered runtime contains unresolved placeholders: {raw}")
PY
echo 'portable install smoke passed'
