#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
INSTALL_SYSTEM=0
INSTALL_HERMES=0
INSTALL_TECTONIC=0
SCAFFOLD=1

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap.sh [options]

Scaffolds private configuration without overwriting existing files.
Options:
  --all                Install supported system packages, Hermes, and Tectonic
  --install-system     Install Git, curl, Python/venv, SQLite CLI, Node.js, and npm
  --install-hermes     Install Hermes from its official installer
  --install-tectonic   Install the pinned checksum-verified Tectonic binary
  --dependencies-only  Install selected dependencies without creating private files
  --help               Show this help

After bootstrap, edit the generated private profiles, set PRIVATE_RESUME_FILE,
and run ./scripts/install.sh. Browser and Photon login remain manual by design.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all) INSTALL_SYSTEM=1; INSTALL_HERMES=1; INSTALL_TECTONIC=1 ;;
    --install-system) INSTALL_SYSTEM=1 ;;
    --install-hermes) INSTALL_HERMES=1 ;;
    --install-tectonic) INSTALL_TECTONIC=1 ;;
    --dependencies-only) SCAFFOLD=0 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$INSTALL_SYSTEM" == "1" ]]; then
  if [[ "$(uname -s)" != "Linux" || ! -r /etc/os-release ]]; then
    echo 'Automatic system-package installation supports Debian/Ubuntu Linux only; see docs/GETTING_STARTED.md.' >&2
    exit 1
  fi
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ;;
    *) echo "Unsupported package manager for ${ID:-unknown}; see docs/GETTING_STARTED.md." >&2; exit 1 ;;
  esac
  if [[ "$EUID" -eq 0 ]]; then
    apt_prefix=()
  elif command -v sudo >/dev/null 2>&1; then
    apt_prefix=(sudo)
  else
    echo 'System package installation requires root or sudo.' >&2
    exit 1
  fi
  "${apt_prefix[@]}" apt-get update
  "${apt_prefix[@]}" apt-get install -y ca-certificates curl git nodejs npm python3 python3-venv sqlite3
fi

for command in git curl python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing $command. Install base dependencies from docs/GETTING_STARTED.md." >&2
    exit 1
  fi
done

if [[ "$INSTALL_HERMES" == "1" ]] && ! command -v hermes >/dev/null 2>&1; then
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$installer"
  bash "$installer"
  rm -f "$installer"
  trap - EXIT
fi

if [[ "$INSTALL_TECTONIC" == "1" ]]; then
  python3 "$ROOT/scripts/install_tectonic.py"
fi

if [[ "$SCAFFOLD" == "0" ]]; then
  echo 'Dependency installation complete; private runtime files were not created.'
  exit 0
fi

JOB_SEARCH_WORKDIR="${JOB_SEARCH_WORKDIR:-$HOME/job-search}"
RESUME_DIR="${RESUME_DIR:-$HOME/job-search-resume}"
mkdir -p "$JOB_SEARCH_WORKDIR" "$RESUME_DIR"
chmod 700 "$JOB_SEARCH_WORKDIR" "$RESUME_DIR"
copy_if_missing() {
  local source="$1" destination="$2"
  if [[ ! -e "$destination" ]]; then
    install -m 600 "$source" "$destination"
    echo "Created $destination"
  else
    echo "Preserved $destination"
  fi
}
copy_if_missing "$ROOT/config/search-profile.example.json" "$JOB_SEARCH_WORKDIR/search-profile.json"
copy_if_missing "$ROOT/config/application-profile.example.json" "$JOB_SEARCH_WORKDIR/application-profile.json"
copy_if_missing "$ROOT/config/sources.example.json" "$ROOT/config/sources.json"

if command -v hermes >/dev/null 2>&1; then
  echo 'Hermes is installed. Run `hermes setup` or `hermes model` to configure inference.'
else
  echo 'Hermes is not installed. Re-run with --install-hermes or follow the official guide.'
fi

echo "Edit $JOB_SEARCH_WORKDIR/search-profile.json and application-profile.json."
echo 'Then set PRIVATE_RESUME_FILE=/path/to/resume.tex and run ./scripts/install.sh.'
python3 "$ROOT/scripts/portable.py" doctor || true
