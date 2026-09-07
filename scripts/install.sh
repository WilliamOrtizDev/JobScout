#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
JOB_SEARCH_HERMES_HOME="${JOB_SEARCH_HERMES_HOME:-$HERMES_HOME}"
JOB_SEARCH_AUTOMATION="${JOB_SEARCH_AUTOMATION:-$ROOT}"
JOB_SEARCH_WORKDIR="${JOB_SEARCH_WORKDIR:-$HOME/job-search}"
RESUME_DIR="${RESUME_DIR:-$HOME/job-search-resume}"
MASTER_RESUME_FILE="${MASTER_RESUME_FILE:-$RESUME_DIR/master-resume.tex}"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
PRIVATE_RESUME_FILE="${PRIVATE_RESUME_FILE:-}"
APPLY_HERMES_PATCH="${APPLY_HERMES_PATCH:-0}"
export HERMES_HOME JOB_SEARCH_HERMES_HOME JOB_SEARCH_AUTOMATION JOB_SEARCH_WORKDIR RESUME_DIR MASTER_RESUME_FILE

copy_if_missing() {
  local source="$1" destination="$2" mode="${3:-600}"
  if [[ -e "$destination" ]]; then
    printf 'Preserved existing %s\n' "$destination"
    return
  fi
  mkdir -p "$(dirname "$destination")"
  chmod 700 "$(dirname "$destination")"
  install -m "$mode" "$source" "$destination"
  printf 'Created %s\n' "$destination"
}

python3 "$ROOT/scripts/validate.py"
"$ROOT/scripts/install_community_sources.sh"
install -d -m 700 \
  "$JOB_SEARCH_WORKDIR" \
  "$RESUME_DIR" \
  "$JOB_SEARCH_HERMES_HOME/scripts" \
  "$JOB_SEARCH_HERMES_HOME/skills/productivity/continuous-job-search"
copy_if_missing "$ROOT/config/search-profile.example.json" "$JOB_SEARCH_WORKDIR/search-profile.json"
copy_if_missing "$ROOT/config/application-profile.example.json" "$JOB_SEARCH_WORKDIR/application-profile.json"
copy_if_missing "$ROOT/config/sources.example.json" "$ROOT/config/sources.json"
python3 "$ROOT/scripts/install_state.py" \
  --database "$JOB_SEARCH_WORKDIR/job-scout.db" \
  --legacy "$JOB_SEARCH_WORKDIR/state.json" >/dev/null

if [[ ! -f "$MASTER_RESUME_FILE" ]]; then
  if [[ -z "$PRIVATE_RESUME_FILE" || ! -f "$PRIVATE_RESUME_FILE" ]]; then
    echo 'Set PRIVATE_RESUME_FILE to the candidate LaTeX resume before completing installation.' >&2
    exit 1
  fi
  mkdir -p "$(dirname "$MASTER_RESUME_FILE")"
  chmod 700 "$(dirname "$MASTER_RESUME_FILE")"
  install -m 600 "$PRIVATE_RESUME_FILE" "$MASTER_RESUME_FILE"
else
  echo "Preserved existing $MASTER_RESUME_FILE"
fi

python3 "$ROOT/scripts/render_runtime.py"

if [[ "$APPLY_HERMES_PATCH" == "1" ]]; then
  if [[ ! -d "$HERMES_AGENT_DIR/.git" ]]; then
    echo "Cannot apply compatibility patch: Hermes source checkout not found at $HERMES_AGENT_DIR" >&2
    exit 1
  fi
  if git -C "$HERMES_AGENT_DIR" apply --reverse --check "$ROOT/patches/hermes-agent-local-fixes.patch" >/dev/null 2>&1; then
    echo 'Optional Hermes compatibility patch already applied'
  elif git -C "$HERMES_AGENT_DIR" apply --check "$ROOT/patches/hermes-agent-local-fixes.patch"; then
    git -C "$HERMES_AGENT_DIR" apply "$ROOT/patches/hermes-agent-local-fixes.patch"
    echo 'Optional Hermes compatibility patch applied'
  else
    echo 'Optional Hermes patch does not apply cleanly; leave APPLY_HERMES_PATCH=0 on current Hermes releases.' >&2
    exit 1
  fi
fi

echo 'Runtime installed. Configure a Hermes model and gateway, run doctor, then create the cron job.'
