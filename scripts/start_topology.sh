#!/usr/bin/env bash
# Launch the Mininet topology (interactive CLI).
#
# Usage:
#   sudo scripts/start_topology.sh
#
# Requires root because Mininet must create network namespaces.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: this script must be run as root (sudo)." >&2
    exit 2
fi

# Clean up any leftover mininet state from a previous crash.
mn -c >/dev/null 2>&1 || true

exec python3 "$REPO_ROOT/src/topology.py"
