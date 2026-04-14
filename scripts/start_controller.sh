#!/usr/bin/env bash
# Launch the Ryu controller for the Network Delay Measurement Tool.
#
# Usage:
#   scripts/start_controller.sh                    # default: LEARN mode
#   PATH_MODE=A scripts/start_controller.sh        # static path A
#   PATH_MODE=B scripts/start_controller.sh        # static path B
#   BLOCK_H2_TO_H4=0 scripts/start_controller.sh   # disable firewall rule
#
# The controller listens on 0.0.0.0:6633 (the OpenFlow default).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

export PATH_MODE="${PATH_MODE:-LEARN}"
export BLOCK_H2_TO_H4="${BLOCK_H2_TO_H4:-1}"

echo "[+] Starting Ryu controller (PATH_MODE=$PATH_MODE, BLOCK_H2_TO_H4=$BLOCK_H2_TO_H4)"
echo "[+] Controller will listen on 0.0.0.0:6633"

exec ryu-manager \
    --ofp-tcp-listen-port 6633 \
    --verbose \
    "$REPO_ROOT/src/controller.py"
