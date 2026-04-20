#!/usr/bin/env bash
# End-to-end: start the Ryu controller in the background, run the test suite,
# then tear everything down.
#
# Usage:
#   sudo scripts/run_all_tests.sh
#
# The Ryu controller is launched as the *invoking* user (not root) because
# Ryu is typically installed into ~/.local and therefore isn't on root's
# import path. Mininet still needs root, so the test runner itself is
# executed with root privileges.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 2
fi

RUN_USER="${SUDO_USER:-$USER}"
RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

mn -c >/dev/null 2>&1 || true
pkill -f ryu-manager >/dev/null 2>&1 || true
sleep 1

echo "[+] Launching Ryu controller as '$RUN_USER'..."
sudo -u "$RUN_USER" -H env \
    HOME="$RUN_USER_HOME" \
    PATH="$RUN_USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    PATH_MODE="${PATH_MODE:-A}" \
    BLOCK_H2_TO_H4="${BLOCK_H2_TO_H4:-1}" \
    ryu-manager --ofp-tcp-listen-port 6633 "$REPO_ROOT/src/controller.py" \
    >/tmp/ryu-controller.log 2>&1 &
RYU_PID=$!

cleanup() {
    echo "[+] stopping controller (pid=$RYU_PID)"
    kill "$RYU_PID" 2>/dev/null || true
    mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Wait for the controller to open its listening socket.
for i in $(seq 1 30); do
    if ss -ltn | grep -q ':6633 '; then break; fi
    sleep 0.3
done

if ! ss -ltn | grep -q ':6633 '; then
    echo "ERROR: controller never started listening on 6633. Log:" >&2
    tail -40 /tmp/ryu-controller.log >&2
    exit 1
fi

echo "[+] Controller up. Launching test scenarios..."
python3 "$REPO_ROOT/src/run_tests.py" --scenario all \
        --results-dir "$REPO_ROOT/results"

echo "[+] All scenarios finished. Reports under: $REPO_ROOT/results/"
echo "[+] Controller log: /tmp/ryu-controller.log"
