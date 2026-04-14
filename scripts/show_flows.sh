#!/usr/bin/env bash
# Dump flow tables of all switches (s1..s4). Use this *while* Mininet is
# running to collect screenshots / logs for the README.
#
# Usage:
#   sudo scripts/show_flows.sh
set -euo pipefail

for sw in s1 s2 s3 s4; do
    echo "==================== $sw flow-table ===================="
    ovs-ofctl -O OpenFlow13 dump-flows "$sw" 2>/dev/null || \
        echo "($sw not present -- is Mininet running?)"
    echo
done
