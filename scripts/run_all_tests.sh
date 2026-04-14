#!/usr/bin/env bash
# End-to-end: start the controller in the background, run the test suite,
# then tear everything down.
#
# Usage:
#   sudo scripts/run_all_tests.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 2
fi

mn -c >/dev/null 2>&1 || true

echo "[+] Launching Ryu controller..."
ryu-manager --ofp-tcp-listen-port 6633 "$REPO_ROOT/src/controller.py" \
    >/tmp/ryu-controller.log 2>&1 &
RYU_PID=$!
trap 'echo "[+] stopping controller (pid=$RYU_PID)"; kill $RYU_PID 2>/dev/null || true; mn -c >/dev/null 2>&1 || true' EXIT

# Wait for the controller to open the listening socket.
for i in $(seq 1 20); do
    if ss -ltn | grep -q ':6633 '; then break; fi
    sleep 0.5
done

echo "[+] Controller up. Launching test scenarios..."
python3 "$REPO_ROOT/src/run_tests.py" --scenario all \
        --results-dir "$REPO_ROOT/results"

echo "[+] All scenarios finished. Reports under: $REPO_ROOT/results/"
