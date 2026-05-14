#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: update.sh must be run from a git checkout."
  exit 1
fi

echo "Updating git repository..."
git pull --ff-only

echo "Updating Python dependencies..."
python3 -m pip install -r requirements.txt

echo "Update complete."
