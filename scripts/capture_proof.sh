#!/usr/bin/env bash
# Capture a complete proof-of-execution bundle for the Network Delay
# Measurement Tool. Produces the following artifacts under
# docs/proof_of_execution/:
#
#   controller.log                   -- Ryu controller log (incl. ARP/stats)
#   openflow.pcap                    -- tcpdump of the OpenFlow channel
#   flows_<N>_<tag>.log              -- ovs-ofctl dump-flows per scenario
#   ping_<label>.log                 -- raw ping transcript per scenario
#   iperf_pathA.log / iperf_pathB.log-- iperf transcripts
#   scenario_*.csv / scenario_*.json -- per-scenario summary reports
#   SUMMARY.md                       -- human-readable index
#
# Usage:
#   sudo scripts/capture_proof.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PROOF_DIR="$REPO_ROOT/docs/proof_of_execution"
RESULTS_DIR="$REPO_ROOT/results"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 2
fi

RUN_USER="${SUDO_USER:-$USER}"
RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

mkdir -p "$PROOF_DIR" "$RESULTS_DIR"

echo "[+] Cleaning prior Mininet / Ryu state"
mn -c >/dev/null 2>&1 || true
pkill -f ryu-manager >/dev/null 2>&1 || true
pkill -f 'tcpdump.*port 6633' >/dev/null 2>&1 || true
sleep 1

echo "[+] Starting OpenFlow pcap capture on loopback (port 6633)"
tcpdump -i lo -w "$PROOF_DIR/openflow.pcap" 'tcp port 6633' \
    >/dev/null 2>&1 &
TCPDUMP_PID=$!

echo "[+] Starting Ryu controller (as '$RUN_USER') -> $PROOF_DIR/controller.log"
sudo -u "$RUN_USER" -H env \
    HOME="$RUN_USER_HOME" \
    PATH="$RUN_USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    PATH_MODE="${PATH_MODE:-A}" \
    BLOCK_H2_TO_H4="${BLOCK_H2_TO_H4:-1}" \
    ryu-manager --verbose --ofp-tcp-listen-port 6633 \
        "$REPO_ROOT/src/controller.py" \
    >"$PROOF_DIR/controller.log" 2>&1 &
RYU_PID=$!

cleanup() {
    echo "[+] Stopping tcpdump (pid=$TCPDUMP_PID)"
    kill "$TCPDUMP_PID" 2>/dev/null || true
    echo "[+] Stopping controller (pid=$RYU_PID)"
    kill "$RYU_PID" 2>/dev/null || true
    wait "$RYU_PID" 2>/dev/null || true
    mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Wait for controller to be listening.
for i in $(seq 1 40); do
    if ss -ltn | grep -q ':6633 '; then break; fi
    sleep 0.3
done
if ! ss -ltn | grep -q ':6633 '; then
    echo "ERROR: controller never started listening on 6633. Log:" >&2
    tail -40 "$PROOF_DIR/controller.log" >&2
    exit 1
fi
echo "[+] Controller up. Running all scenarios..."

python3 "$REPO_ROOT/src/run_tests.py" \
    --scenario all \
    --results-dir "$RESULTS_DIR" \
    --proof-dir "$PROOF_DIR" \
    2>&1 | tee "$PROOF_DIR/run_tests_stdout.log"

echo "[+] Generating SUMMARY.md ..."
python3 "$REPO_ROOT/scripts/proof_summary.py" \
    --proof-dir "$PROOF_DIR" \
    --results-dir "$RESULTS_DIR"

# Make the artifacts owned by the invoking user rather than root.
chown -R "$RUN_USER":"$RUN_USER" "$PROOF_DIR" "$RESULTS_DIR" || true

echo
echo "[+] Proof-of-execution bundle complete:"
ls -la "$PROOF_DIR"
echo
echo "    -> Open: $PROOF_DIR/SUMMARY.md"
