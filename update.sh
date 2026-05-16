#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# ---------------------------------------------------------------------------
# Configuration — override via environment variables if needed
# ---------------------------------------------------------------------------
SERVICE_NAME="${SERVICE_NAME:-apple-home-key-reader}"
BACKUP_DIR="${SCRIPT_DIR}/.backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="${BACKUP_DIR}/${TIMESTAMP}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"  # number of old backups to retain

# All state/config files that must survive an update
STATE_FILES=(
  "configuration.json"
  "hap.state"
  "homekey.json"
  "known_nfc_uids.json"
  "new_nfc_uids.json"
  "homekey_user_names.json"
  "access_log.jsonl"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[$(date +%H:%M:%S)] $*"; }
err()  { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; }

service_active() {
  systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null
}

service_enabled() {
  systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null
}

do_rollback() {
  log "Rolling back to backup ${TIMESTAMP}..."

  # Restore git state (code)
  if [[ -f "${BACKUP_PATH}/GIT_HASH" ]]; then
    git checkout "$(cat "${BACKUP_PATH}/GIT_HASH")" --quiet
    log "  Restored code to $(cat "${BACKUP_PATH}/GIT_HASH")"
  fi

  # Restore state and config files
  for f in "${STATE_FILES[@]}"; do
    src="${BACKUP_PATH}/${f}"
    if [[ -f "${src}" ]]; then
      cp "${src}" "${SCRIPT_DIR}/${f}"
      log "  Restored ${f}"
    fi
  done

  # Reinstall original dependencies
  if [[ -f "${BACKUP_PATH}/requirements.txt" ]]; then
    log "  Reinstalling original dependencies..."
    PIP_FLAGS=(--quiet)
    if python3 -m pip install --help 2>/dev/null | grep -q "break-system-packages"; then
      PIP_FLAGS+=(--break-system-packages)
    fi
    python3 -m pip install -r "${BACKUP_PATH}/requirements.txt" "${PIP_FLAGS[@]}"
  fi

  # Restart service after rollback
  if service_active || service_enabled; then
    log "  Restarting service ${SERVICE_NAME}..."
    systemctl restart "${SERVICE_NAME}" && log "  Service restarted." \
      || err "Service failed to restart after rollback — check: journalctl -u ${SERVICE_NAME}"
  fi

  log "Rollback complete. Backup preserved at: ${BACKUP_PATH}"
}

prompt_rollback() {
  echo ""
  err "The update encountered an error."
  echo "A full backup is available at: ${BACKUP_PATH}"
  echo ""
  read -rp "Roll back to the pre-update state? [y/N] " answer
  if [[ "${answer,,}" == "y" ]]; then
    do_rollback
  else
    log "Skipping rollback. Manual recovery backup is at: ${BACKUP_PATH}"
  fi
  exit 1
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  err "update.sh must be run from within a git checkout."
  exit 1
fi

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
log "Creating backup at ${BACKUP_PATH}..."
mkdir -p "${BACKUP_PATH}"

# Save current git hash so rollback can restore the exact code version
git rev-parse HEAD > "${BACKUP_PATH}/GIT_HASH"
log "  Current commit: $(cat "${BACKUP_PATH}/GIT_HASH")"

# Save current requirements so pip can reinstall the old versions on rollback
cp requirements.txt "${BACKUP_PATH}/requirements.txt"

# Copy all state and config files
for f in "${STATE_FILES[@]}"; do
  if [[ -f "${SCRIPT_DIR}/${f}" ]]; then
    cp "${SCRIPT_DIR}/${f}" "${BACKUP_PATH}/${f}"
    log "  Backed up ${f}"
  fi
done

log "Backup complete."

# ---------------------------------------------------------------------------
# Stop service before update
# ---------------------------------------------------------------------------
SERVICE_WAS_RUNNING=false
if service_active; then
  SERVICE_WAS_RUNNING=true
  log "Stopping service ${SERVICE_NAME}..."
  systemctl stop "${SERVICE_NAME}" || { err "Failed to stop service."; prompt_rollback; }
  log "Service stopped."
fi

# Any error from here until service start triggers a rollback prompt
trap 'prompt_rollback' ERR

# ---------------------------------------------------------------------------
# Update code and dependencies
# ---------------------------------------------------------------------------
log "Pulling latest changes from git..."
# If in detached HEAD state, reattach to main before pulling
if ! git symbolic-ref --quiet HEAD >/dev/null 2>&1; then
  log "Detached HEAD detected — checking out main branch..."
  git checkout main
fi
git pull --ff-only

log "Updating Python dependencies..."
PIP_FLAGS=()
# Newer Debian/Raspberry Pi OS versions enforce PEP 668 (externally managed env).
# If pip supports --break-system-packages (pip >= 23.1), use it — safe on a dedicated device.
if python3 -m pip install --help 2>/dev/null | grep -q "break-system-packages"; then
  PIP_FLAGS+=(--break-system-packages)
fi
python3 -m pip install --upgrade "${PIP_FLAGS[@]}" -r requirements.txt

trap - ERR  # clear the trap before attempting service start

# ---------------------------------------------------------------------------
# Start / restart service
# ---------------------------------------------------------------------------
if [[ "${SERVICE_WAS_RUNNING}" == "true" ]] || service_enabled; then
  log "Starting service ${SERVICE_NAME}..."
  if ! systemctl start "${SERVICE_NAME}"; then
    err "Service failed to start after update."
    echo "A full backup is available at: ${BACKUP_PATH}"
    echo ""
    read -rp "Roll back to the pre-update state? [y/N] " answer
    if [[ "${answer,,}" == "y" ]]; then
      do_rollback
    else
      log "Skipping rollback. Run 'journalctl -u ${SERVICE_NAME}' to diagnose."
      log "Backup is at: ${BACKUP_PATH}"
    fi
    exit 1
  fi
  log "Service started successfully."
else
  log "No systemd service detected — skipping service restart."
  log "Start the application manually when ready."
fi

# ---------------------------------------------------------------------------
# Prune old backups (keep the N most recent)
# ---------------------------------------------------------------------------
mapfile -t old_backups < <(ls -dt "${BACKUP_DIR}"/[0-9]* 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))")
if [[ ${#old_backups[@]} -gt 0 ]]; then
  log "Pruning ${#old_backups[@]} old backup(s)..."
  rm -rf "${old_backups[@]}"
fi

log "Update complete."
