#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${JOB_SCOUT_COMMUNITY_VENV:-$ROOT/.venv-community}"
PYTHON="${JOB_SCOUT_COMMUNITY_PYTHON:-python3}"

"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check --quiet \
  -r "$ROOT/requirements-community.txt"
"$VENV/bin/python" -c 'import duckdb; assert duckdb.__version__ == "1.5.5"'