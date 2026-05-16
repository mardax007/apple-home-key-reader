#!/usr/bin/env bash
# bootstrap_update.sh — one-shot script to adopt the update.sh workflow on an
# existing installation that was set up without git or without update.sh.
#
# Usage:
#   bash bootstrap_update.sh [--install-dir /path/to/installation]
#
# What it does:
#   1. If run from inside an existing git checkout: just pulls the latest
#      update.sh and makes it executable. Done.
#   2. If run from a non-git directory: clones the repo next to the current
#      installation, migrates your state files into it, and runs the first
#      update from there.
set -euo pipefail

REPO_URL="https://github.com/mardax007/apple-home-key-reader.git"
DEFAULT_INSTALL_DIR="/opt/apple-home-key-reader"

# State/config files that must be migrated from the old installation
STATE_FILES=(
  "configuration.json"
  "hap.state"
  "homekey.json"
  "known_nfc_uids.json"
  "new_nfc_uids.json"
  "homekey_user_names.json"
  "access_log.jsonl"
)

log() { echo "[$(date +%H:%M:%S)] $*"; }
err() { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; }

# Parse arguments
INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    *) err "Unknown argument: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Case 1: already inside a git checkout — just make sure update.sh is current
# ---------------------------------------------------------------------------
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  REPO_ROOT="$(git rev-parse --show-toplevel)"
  log "Detected existing git checkout at ${REPO_ROOT}"

  log "Pulling latest changes (including update.sh)..."
  cd "${REPO_ROOT}"
  git pull --ff-only

  chmod +x "${REPO_ROOT}/update.sh"
  log "update.sh is now up-to-date and executable."
  log "Run './update.sh' for future updates."
  exit 0
fi

# ---------------------------------------------------------------------------
# Case 2: non-git installation — clone repo and migrate state files
# ---------------------------------------------------------------------------
CURRENT_DIR="$(pwd)"

log "No git checkout detected in current directory."
log "This script will:"
log "  1. Clone the repository to ${INSTALL_DIR}"
log "  2. Migrate your state/config files from ${CURRENT_DIR}"
log "  3. Set up update.sh for future use"
echo ""
read -rp "Continue? [y/N] " answer
[[ "${answer,,}" == "y" ]] || { log "Aborted."; exit 0; }

# Verify git is available
if ! command -v git >/dev/null 2>&1; then
  err "git is not installed. Install it first (e.g. 'sudo apt install git') and re-run."
  exit 1
fi

# Verify target directory is safe
if [[ -d "${INSTALL_DIR}" ]]; then
  err "Target directory ${INSTALL_DIR} already exists. Remove it or choose a different --install-dir."
  exit 1
fi

# Clone the repository
log "Cloning repository to ${INSTALL_DIR}..."
git clone "${REPO_URL}" "${INSTALL_DIR}"

# Migrate state and config files from the old installation
log "Migrating state files from ${CURRENT_DIR}..."
migrated=0
for f in "${STATE_FILES[@]}"; do
  src="${CURRENT_DIR}/${f}"
  if [[ -f "${src}" ]]; then
    cp "${src}" "${INSTALL_DIR}/${f}"
    log "  Migrated ${f}"
    ((migrated++))
  fi
done

if [[ ${migrated} -eq 0 ]]; then
  log "No state files found in ${CURRENT_DIR} — starting fresh."
fi

# Install / upgrade dependencies in the new location
log "Installing Python dependencies..."
cd "${INSTALL_DIR}"
python3 -m pip install -r requirements.txt

chmod +x update.sh

log ""
log "Bootstrap complete!"
log "Installation is at: ${INSTALL_DIR}"
log "Run '${INSTALL_DIR}/update.sh' for future updates."
log ""
log "Next steps:"
log "  1. Edit ${INSTALL_DIR}/configuration.json to verify your settings."
if [[ ${migrated} -gt 0 ]]; then
  log "  2. Your existing state files were migrated — the app should pick up"
  log "     your existing HomeKit pairing and NFC tags automatically."
fi
log "  3. (Re)start your service pointing to the new directory."
