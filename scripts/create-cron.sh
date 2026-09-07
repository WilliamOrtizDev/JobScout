#!/usr/bin/env bash
set -euo pipefail
: "${PHOTON_TARGET:?Set PHOTON_TARGET, for example photon:+1XXXXXXXXXX}"
JOB_SEARCH_WORKDIR="${JOB_SEARCH_WORKDIR:-$HOME/job-search}"
HERMES_PROFILE="${HERMES_PROFILE:-}"
BASE_HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [[ -z "${JOB_SEARCH_HERMES_HOME:-}" ]]; then
  if [[ -n "$HERMES_PROFILE" ]]; then
    JOB_SEARCH_HERMES_HOME="$BASE_HERMES_HOME/profiles/$HERMES_PROFILE"
  else
    JOB_SEARCH_HERMES_HOME="$BASE_HERMES_HOME"
  fi
fi
export HERMES_HOME="$JOB_SEARCH_HERMES_HOME"
JOB_NAME="${JOB_NAME:-Contract job scout}"
PROMPT_FILE="${JOB_SEARCH_PROMPT:-$JOB_SEARCH_WORKDIR/cron-prompt.md}"
if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Missing rendered cron prompt: $PROMPT_FILE. Run scripts/install.sh first." >&2
  exit 1
fi
if [[ "$(hermes cron list --all)" == *"$JOB_NAME"* ]]; then
  echo 'A matching cron job already exists; refusing to create a duplicate.' >&2
  exit 1
fi
prompt="$(<"$PROMPT_FILE")"
hermes cron create 'every 15m' "$prompt" \
  --name "$JOB_NAME" \
  --deliver "$PHOTON_TARGET" \
  --failure-deliver "$PHOTON_TARGET" \
  --skill continuous-job-search \
  --script job-scout-shadow.py \
  --workdir "$JOB_SEARCH_WORKDIR"
