#!/usr/bin/env python
"""
Automated Test Runner
---------------------
Spawns the Mininet topology (via src/topology.py), runs pings between hosts
from *inside* Mininet (using host.cmd('ping ...')), and feeds the output to
DelayResult.parse() in src/delay_measurement.py.

Four scenarios are exercised:

  1. baseline_learning  -- controller in LEARN mode, measure h1->h4 RTT
                           over whichever path the learning switch chooses.
  2. path_A             -- static flow rules pin traffic to the upper path.
  3. path_B             -- static flow rules pin traffic to the lower path.
  4. link_failure       -- start on path A, take link s1<->s2 down,
                           re-measure to show that traffic either fails
                           (no backup installed) or succeeds after the
                           controller reinstalls a fallback.
  5. blocked_h2_h4      -- ICMP h2 -> h4 must fail (firewall demo),
                           while h2 -> h3 still succeeds.

NOTE:
  This script must be run as root (sudo) because Mininet needs to create
  network namespaces and veth pairs. Before running, you must start the
  Ryu controller in another terminal. See README.md.

Usage:
    sudo python3 src/run_tests.py
    sudo python3 src/run_tests.py --scenario path_A
"""

import argparse
import os
import sys
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info

# Make src importable when this script is launched from the repo root
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from topology import DelayTopo                              # noqa: E402
from delay_measurement import (                             # noqa: E402
    DelayResult, save_results, print_comparison,
)


CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6633
PING_COUNT = 15
PING_INTERVAL = 0.3

# --------------------------------------------------------------------------- #
# Proof-of-execution capture helpers                                          #
# --------------------------------------------------------------------------- #
PROOF_DIR = None


def _proof_set_dir(path):
    global PROOF_DIR
    if path:
        PROOF_DIR = os.path.abspath(path)
        os.makedirs(PROOF_DIR, exist_ok=True)
        info(f'    [proof] writing artifacts to {PROOF_DIR}\n')


def _proof_write(filename, content, append=False):
    if not PROOF_DIR:
        return
    mode = 'a' if append else 'w'
    with open(os.path.join(PROOF_DIR, filename), mode) as f:
        f.write(content)
        if not content.endswith('\n'):
            f.write('\n')


def _dump_flows(tag):
    """Write `ovs-ofctl dump-flows` for every switch into a single file."""
    if not PROOF_DIR:
        return
    import subprocess
    chunks = [f'### Flow tables snapshot: {tag}\n']
    for sw in ('s1', 's2', 's3', 's4'):
        try:
            out = subprocess.check_output(
                ['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', sw],
                text=True,
            )
        except Exception as e:
            out = f'(error dumping {sw}: {e})\n'
        chunks.append(f'\n--- {sw} ---\n{out}')
    _proof_write(f'flows_{tag}.log', ''.join(chunks))


def _mn_ping(src, dst_ip: str, count: int = PING_COUNT,
             interval: float = PING_INTERVAL, label: str = '') -> DelayResult:
    """Run ping inside a Mininet host and parse the output."""
    info(f'    [{label}] {src.name} -> {dst_ip} '
         f'({count} pkts @ {interval}s)\n')
    cmd = f'ping -c {count} -i {interval} -W 2 {dst_ip}'
    out = src.cmd(cmd)
    _proof_write(
        f'ping_{label}.log',
        f'# command: {src.name} {cmd}\n{out}',
    )
    return DelayResult.parse(out, label=label, src=src.name,
                             dst=dst_ip, count=count)


def _mn_iperf(server, client, dst_ip: str, label: str,
              seconds: int = 5) -> str:
    """Run a TCP iperf between two hosts and capture the transcript."""
    info(f'    [iperf] {client.name} -> {server.name} ({seconds}s)\n')
    server.cmd(f'iperf -s -D > /tmp/iperf_{label}_server.log 2>&1')
    time.sleep(0.5)
    out = client.cmd(f'iperf -c {dst_ip} -t {seconds}')
    server.cmd('kill %iperf >/dev/null 2>&1 ; killall -q iperf')
    srv_log = ''
    try:
        with open(f'/tmp/iperf_{label}_server.log') as f:
            srv_log = f.read()
    except Exception:
        pass
    transcript = (
        f'# iperf client: {client.name} -> {dst_ip} ({seconds}s)\n'
        f'{out}\n--- server log ---\n{srv_log}\n'
    )
    _proof_write(f'iperf_{label}.log', transcript)
    return out


def _start_net():
    net = Mininet(
        topo=DelayTopo(),
        switch=OVSSwitch,
        link=TCLink,
        controller=None,
        autoSetMacs=False,
        autoStaticArp=False,
    )
    net.addController(
        'c0',
        controller=RemoteController,
        ip=CONTROLLER_IP,
        port=CONTROLLER_PORT,
    )
    net.start()
    # Give the controller a chance to push table-miss + static flows
    info('    [*] waiting 3s for flow rules to settle...\n')
    time.sleep(3)
    return net


def scenario_baseline(results_dir: str):
    """Verify reachability and measure RTT on the controller's default path."""
    info('\n=== Scenario 1: baseline (controller default path A) ===\n')
    net = _start_net()
    try:
        _dump_flows('1_baseline_initial')
        h1, h4 = net.get('h1', 'h4')
        # Prime ARP so the first probe measures the data path, not ARP.
        h1.cmd('ping -c 2 -W 1 10.0.0.4')
        r = _mn_ping(h1, '10.0.0.4', label='baseline_h1_h4')
        print(r.summary())
        _dump_flows('1_baseline_after_ping')
        save_results([r], results_dir, 'scenario_1_baseline')
    finally:
        net.stop()


def scenario_path_compare(results_dir: str):
    """Compare path A vs path B.

    This scenario assumes the user starts the controller twice (once with
    PATH_MODE=A, once with PATH_MODE=B) and runs this scenario each time.
    For a fully-automated run, use --scenario path_compare_auto which uses
    `ovs-ofctl` to swap the rules directly.
    """
    info('\n=== Scenario 2: path comparison (run twice with PATH_MODE=A/B) ===\n')
    net = _start_net()
    try:
        h1, h4 = net.get('h1', 'h4')
        h1.cmd('ping -c 2 -W 1 10.0.0.4')
        mode = os.environ.get('PATH_MODE', 'LEARN')
        r = _mn_ping(h1, '10.0.0.4', label=f'path_{mode}_h1_h4')
        print(r.summary())
        save_results([r], results_dir, f'scenario_2_path_{mode}')
    finally:
        net.stop()


def scenario_path_compare_auto(results_dir: str):
    """Use ovs-ofctl to install static paths A then B back-to-back."""
    info('\n=== Scenario 2b: automatic path A vs B (ovs-ofctl override) ===\n')
    net = _start_net()
    try:
        h1 = net.get('h1')

        # Baseline using whatever the controller provided
        h1.cmd('ping -c 2 -W 1 10.0.0.4')

        info('    [*] installing PATH A via ovs-ofctl...\n')
        _install_path_ofctl('A')
        time.sleep(1)
        _dump_flows('2_pathA_active')
        r_a = _mn_ping(h1, '10.0.0.4', label='path_A_h1_h4')
        print(r_a.summary())

        info('    [*] installing PATH B via ovs-ofctl...\n')
        _install_path_ofctl('B')
        time.sleep(1)
        _dump_flows('2_pathB_active')
        r_b = _mn_ping(h1, '10.0.0.4', label='path_B_h1_h4')
        print(r_b.summary())

        print_comparison([r_a, r_b])
        save_results([r_a, r_b], results_dir, 'scenario_2b_path_compare')
    finally:
        net.stop()


SCENARIO_COOKIE = '0x1234'


def _install_path_ofctl(mode: str):
    """Directly push OpenFlow rules with ovs-ofctl for repeatable tests.

    Scenario-installed rules are tagged with a distinctive cookie so we can
    remove them without touching the controller-installed defaults or
    firewall drops.
    """
    import subprocess

    # (switch, match, out_port)
    rules_A = [
        ('s1', 'ip,nw_src=10.0.0.1,nw_dst=10.0.0.4', 2),
        ('s2', 'ip,nw_src=10.0.0.1,nw_dst=10.0.0.4', 2),
        ('s4', 'ip,nw_src=10.0.0.1,nw_dst=10.0.0.4', 1),
        ('s4', 'ip,nw_src=10.0.0.4,nw_dst=10.0.0.1', 2),
        ('s2', 'ip,nw_src=10.0.0.4,nw_dst=10.0.0.1', 1),
        ('s1', 'ip,nw_src=10.0.0.4,nw_dst=10.0.0.1', 1),
    ]
    rules_B = [
        ('s1', 'ip,nw_src=10.0.0.1,nw_dst=10.0.0.4', 3),
        ('s3', 'ip,nw_src=10.0.0.1,nw_dst=10.0.0.4', 4),
        ('s4', 'ip,nw_src=10.0.0.1,nw_dst=10.0.0.4', 1),
        ('s4', 'ip,nw_src=10.0.0.4,nw_dst=10.0.0.1', 3),
        ('s3', 'ip,nw_src=10.0.0.4,nw_dst=10.0.0.1', 3),
        ('s1', 'ip,nw_src=10.0.0.4,nw_dst=10.0.0.1', 1),
    ]

    # Wipe any previous scenario rules (matched by cookie, leaves the
    # controller's own flows intact).
    for sw in ('s1', 's2', 's3', 's4'):
        subprocess.run(
            ['ovs-ofctl', '-O', 'OpenFlow13', 'del-flows', sw,
             f'cookie={SCENARIO_COOKIE}/-1'],
            check=False,
        )

    rules = rules_A if mode == 'A' else rules_B
    for sw, match, out_port in rules:
        flow = (f'cookie={SCENARIO_COOKIE},priority=200,{match},'
                f'actions=output:{out_port}')
        subprocess.run(
            ['ovs-ofctl', '-O', 'OpenFlow13', 'add-flow', sw, flow],
            check=False,
        )


def scenario_link_failure(results_dir: str):
    """Show delay behaviour when a link on path A is torn down."""
    info('\n=== Scenario 3: link failure (path A broken mid-flight) ===\n')
    net = _start_net()
    try:
        h1 = net.get('h1')
        h1.cmd('ping -c 2 -W 1 10.0.0.4')

        info('    [*] installing PATH A via ovs-ofctl...\n')
        _install_path_ofctl('A')
        time.sleep(1)
        _dump_flows('3_linkfail_before')

        r_before = _mn_ping(h1, '10.0.0.4', label='before_failure')
        print(r_before.summary())

        info('    [*] TAKING DOWN link s1<->s2 (path A upper leg)...\n')
        net.configLinkStatus('s1', 's2', 'down')
        time.sleep(1)
        _dump_flows('3_linkfail_during')

        r_broken = _mn_ping(h1, '10.0.0.4', count=8, label='during_failure')
        print(r_broken.summary())

        info('    [*] FAILOVER: switching to PATH B via ovs-ofctl...\n')
        _install_path_ofctl('B')
        time.sleep(1)
        _dump_flows('3_linkfail_after_failover')

        r_after = _mn_ping(h1, '10.0.0.4', label='after_failover')
        print(r_after.summary())

        # Bring the link back for cleanliness
        net.configLinkStatus('s1', 's2', 'up')

        print_comparison([r_before, r_broken, r_after])
        save_results([r_before, r_broken, r_after],
                     results_dir, 'scenario_3_link_failure')
    finally:
        net.stop()


def scenario_blocked(results_dir: str):
    """ICMP h2 -> h4 should be DROPPED by the firewall flow rule."""
    info('\n=== Scenario 4: firewall (allowed vs blocked) ===\n')
    net = _start_net()
    try:
        h2 = net.get('h2')
        h3 = net.get('h3')

        h2.cmd('ping -c 2 -W 1 10.0.0.4')   # warm up ARP
        h2.cmd('ping -c 2 -W 1 10.0.0.3')
        _dump_flows('4_firewall_initial')

        r_allowed = _mn_ping(h2, '10.0.0.3', count=10, label='allowed_h2_h3')
        print(r_allowed.summary())

        r_blocked = _mn_ping(h2, '10.0.0.4', count=10, label='blocked_h2_h4')
        print(r_blocked.summary())
        _dump_flows('4_firewall_after_ping')

        print_comparison([r_allowed, r_blocked])
        save_results([r_allowed, r_blocked],
                     results_dir, 'scenario_4_blocked')
    finally:
        net.stop()


def scenario_iperf(results_dir: str):
    """TCP throughput on Path A vs Path B using iperf."""
    info('\n=== Scenario 5: iperf throughput (Path A vs Path B) ===\n')
    net = _start_net()
    try:
        h1, h4 = net.get('h1', 'h4')
        h1.cmd('ping -c 2 -W 1 10.0.0.4')   # ARP warm-up

        info('    [*] installing PATH A via ovs-ofctl...\n')
        _install_path_ofctl('A')
        time.sleep(1)
        a_out = _mn_iperf(h4, h1, '10.0.0.4', label='pathA', seconds=5)
        info(f'    [iperf A] {a_out.strip().splitlines()[-1]}\n')

        info('    [*] installing PATH B via ovs-ofctl...\n')
        _install_path_ofctl('B')
        time.sleep(1)
        b_out = _mn_iperf(h4, h1, '10.0.0.4', label='pathB', seconds=5)
        info(f'    [iperf B] {b_out.strip().splitlines()[-1]}\n')

        _dump_flows('5_iperf_final')
    finally:
        net.stop()


SCENARIOS = {
    'baseline': scenario_baseline,
    'path_compare': scenario_path_compare,
    'path_compare_auto': scenario_path_compare_auto,
    'link_failure': scenario_link_failure,
    'blocked': scenario_blocked,
    'iperf': scenario_iperf,
}


def main(argv=None):
    p = argparse.ArgumentParser(description='SDN delay measurement test runner')
    p.add_argument('--scenario', choices=list(SCENARIOS) + ['all'],
                   default='all',
                   help='which test scenario to run (default: all)')
    p.add_argument('--results-dir', default='results',
                   help='directory for JSON/CSV reports')
    p.add_argument('--proof-dir', default=None,
                   help='if set, write flow-table dumps, ping and iperf '
                        'transcripts into this directory (proof-of-execution)')
    args = p.parse_args(argv)

    setLogLevel('info')

    if os.geteuid() != 0:
        print('ERROR: run_tests.py must be run as root (sudo).',
              file=sys.stderr)
        return 2

    _proof_set_dir(args.proof_dir)

    if args.scenario == 'all':
        for name in ('baseline', 'path_compare_auto',
                     'link_failure', 'blocked', 'iperf'):
            SCENARIOS[name](args.results_dir)
    else:
        SCENARIOS[args.scenario](args.results_dir)

    return 0


if __name__ == '__main__':
    sys.exit(main())
