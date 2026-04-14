# SDN Network Delay Measurement Tool

> Problem #15 – *Measure and analyze latency between hosts.*
> *Use ping for delay measurement; record RTT values; compare across paths;
> analyze delay variations.*

An individual SDN project built with **Mininet** + **Ryu (OpenFlow 1.3)** that
measures network delay between hosts, installs explicit flow rules to steer
traffic along different paths, and analyzes how path choice and link failure
change the observed RTT.

---

## 1. Problem Statement

Modern networks frequently offer more than one path between a given pair of
endpoints. The delay (round-trip time) a packet experiences depends on which
path the control plane chooses and on the state of every link along that path.
The goal of this project is to:

1. Build an SDN network in Mininet with **multiple paths of different
   latencies** between two hosts.
2. Use a **Ryu OpenFlow 1.3 controller** to:
   - Handle `packet_in` events for unknown flows (L2 learning fallback).
   - Install **explicit match/action flow rules** that pin traffic to a
     chosen path.
   - Install a **firewall / ACL** rule to drop ICMP between two specific
     hosts (for the "allowed vs blocked" scenario).
   - Periodically poll **flow-table and port statistics** for observability.
3. Build a Python **delay measurement tool** that drives `ping`, parses its
   output, and produces CSV / JSON reports comparing paths.
4. Run test scenarios that exercise the "normal vs failure" and "allowed vs
   blocked" cases required by the evaluation rubric.

---

## 2. Repository Layout

```
Network-Delay-Measurment-Tool/
├── README.md                    <- this file
├── requirements.txt             <- Python deps (Ryu + pinned transitive libs)
├── src/
│   ├── topology.py              <- Mininet diamond topology (TCLink + delays)
│   ├── controller.py            <- Ryu OpenFlow 1.3 controller
│   ├── delay_measurement.py     <- ping driver + RTT parser + reporter
│   ├── run_tests.py             <- automated scenario runner
│   └── test_parser.py           <- regression tests for the RTT parser
├── scripts/
│   ├── start_controller.sh      <- launch Ryu
│   ├── start_topology.sh        <- launch Mininet CLI
│   ├── run_all_tests.sh         <- start controller + run every scenario
│   └── show_flows.sh            <- ovs-ofctl dump-flows for every switch
├── results/                     <- CSV / JSON reports land here after runs
└── docs/
    └── screenshots/             <- place your demo screenshots here
```

---

## 3. Topology & Design Justification

```
                          [s2]
                         /    \
                   5 ms /      \ 5 ms
                       /        \
              h1 --- [s1]      [s4] --- h4
                  1ms  \        /  1ms
                   15ms \      / 15ms
                         \    /
                          [s3]
                          /  \
                    1ms  /    \  1ms
                        h2    h3
```

| Link        | Bandwidth | Delay  | Purpose                                   |
|-------------|-----------|--------|-------------------------------------------|
| h*–sN       | 10 Mbps   | 1 ms   | low-latency access links                  |
| s1–s2, s2–s4| 10 Mbps   | 5 ms   | **Path A** (upper, low latency)           |
| s1–s3, s3–s4| 10 Mbps   | 15 ms  | **Path B** (lower, high latency)          |

**Why this topology?**

- The two disjoint paths between `h1` and `h4` are what make delay
  *measurement* interesting — without multiple paths there is nothing to
  compare.
- `h2` and `h3` share switch `s3`, so they give a short intra-switch
  reference for the firewall scenario (allowed `h2→h3` vs blocked `h2→h4`).
- Every inter-switch link uses Mininet's `TCLink` with a `delay=` parameter
  so the RTT differences we measure are *real* tc-netem delays, not
  simulated.
- The 3× delay ratio between Path A and Path B (~10 ms vs ~30 ms one-way)
  is large enough to be unambiguous even with scheduler jitter inside a VM.

---

## 4. SDN Logic / Controller Design

`src/controller.py` is a Ryu app (`DelayMeasurementController`) with three
layers:

### 4.1 Table-miss + L2 learning fallback
- On every `OFPSwitchFeatures` event (switch connect) a priority-0 rule is
  installed that forwards unmatched packets to the controller.
- On every `OFPPacketIn` the controller learns the source MAC → port
  mapping and installs a priority-10 flow with a 30 s idle / 120 s hard
  timeout, so subsequent frames stay on the data plane.

### 4.2 Explicit path steering (match + action)
When the controller is started with `PATH_MODE=A` or `PATH_MODE=B`, it
pre-installs priority-100 / priority-110 IPv4 + ICMP flows at every switch
that pin `h1 ↔ h4` traffic onto the chosen path. Example (path A, on `s1`):

```text
priority=110, match: eth_type=0x0800, ip_proto=1,
              ipv4_src=10.0.0.1, ipv4_dst=10.0.0.4
actions:      output:2
```

This directly demonstrates **flow-rule design (match + action)** and
**priorities** — higher-priority static rules override the L2 fallback.

### 4.3 Firewall / ACL drop rule
If `BLOCK_H2_TO_H4=1` (default), the controller installs priority-500 flows
with an **empty action list** (OpenFlow's "drop") at every switch:

```text
priority=500, match: eth_type=0x0800, ip_proto=1,
              ipv4_src=10.0.0.2, ipv4_dst=10.0.0.4
actions:      (none -> drop)
```

This is the "allowed vs blocked" test scenario in Section 7.

### 4.4 Stats polling
A Ryu `hub.spawn` thread sends `OFPFlowStatsRequest` + `OFPPortStatsRequest`
every 10 s to every connected switch and logs the top entries. This is what
the **Monitoring / Logging** evaluation component asks for.

---

## 5. Requirements & Setup

Tested on **Ubuntu 20.04** and **Ubuntu 22.04** with Python 3.8/3.10.
An Ubuntu VM is the easiest way to run this (Mininet does not work on
Windows/macOS without a VM).

### 5.1 Install system packages

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-pip iputils-ping \
                    tcpdump wireshark iperf3
```

### 5.2 Install Python dependencies

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

### 5.2a Python 3.10 (Ubuntu 22.04) post-install fix

Ryu 4.34 was last released in 2020 and depends on two Python APIs that
broke in Python 3.10. After `pip install -r requirements.txt`, run:

```bash
# Add back the ALREADY_HANDLED constant that eventlet 0.33+ removed
# (Ryu still imports it at class-definition time, but we don't use the
# feature). The sed uses python to resolve the correct site-packages path.
EVENTLET_WSGI=$(python3 -c 'import eventlet.wsgi, os; print(eventlet.wsgi.__file__)')
grep -q 'ALREADY_HANDLED' "$EVENTLET_WSGI" || \
    echo 'ALREADY_HANDLED = []' >> "$EVENTLET_WSGI"
```

If `pip3 install --user` was used, also make sure `~/.local/bin` is on
`PATH` so `ryu-manager` resolves:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 5.3 Verify

```bash
# Mininet basic test
sudo mn --test pingall

# Ryu imports cleanly
ryu-manager --version
```

---

## 6. Execution Steps

You need **two terminals**: one for the controller, one for Mininet.

### 6.1 Manual / interactive demo

**Terminal 1** – start the controller (learning-switch mode, firewall on):

```bash
./scripts/start_controller.sh
```

To pin traffic to Path A or Path B instead of learning:

```bash
PATH_MODE=A ./scripts/start_controller.sh      # upper path
PATH_MODE=B ./scripts/start_controller.sh      # lower path
BLOCK_H2_TO_H4=0 ./scripts/start_controller.sh # disable firewall
```

**Terminal 2** – start the topology (Mininet CLI):

```bash
sudo ./scripts/start_topology.sh
```

Inside the Mininet CLI, try:

```text
mininet> pingall
mininet> h1 ping -c 10 h4
mininet> h2 ping -c 5  h4      # blocked by firewall rule
mininet> h2 ping -c 5  h3      # allowed
mininet> h1 python3 /path/to/src/delay_measurement.py 10.0.0.4 -c 20
mininet> iperf h1 h4
```

**Terminal 3** – inspect flow tables while the demo is running:

```bash
sudo ./scripts/show_flows.sh
```

### 6.2 Fully-automated scenario runner

With the controller running in terminal 1, run every scenario end-to-end:

```bash
sudo python3 src/run_tests.py --scenario all --results-dir results
```

Or run a single scenario:

```bash
sudo python3 src/run_tests.py --scenario baseline
sudo python3 src/run_tests.py --scenario path_compare_auto
sudo python3 src/run_tests.py --scenario link_failure
sudo python3 src/run_tests.py --scenario blocked
```

The helper `scripts/run_all_tests.sh` starts the controller, runs every
scenario, and shuts everything down cleanly:

```bash
sudo ./scripts/run_all_tests.sh
```

---

## 7. Test Scenarios

| # | Name                 | What it shows                                            |
|---|----------------------|----------------------------------------------------------|
| 1 | `baseline`           | L2 learning switch; verifies reachability + baseline RTT |
| 2 | `path_compare_auto`  | Same src/dst, forced onto Path A then Path B             |
| 3 | `link_failure`       | Path A active, `s1–s2` brought down, failover to Path B  |
| 4 | `blocked`            | `h2 → h3` allowed, `h2 → h4` dropped by firewall rule    |

### 7.1 Expected output — path comparison

`h1 → h4` with **Path A active** (upper, 2 × 5 ms + 2 × 1 ms access):

```
--- 10.0.0.4 ping statistics ---
15 packets transmitted, 15 received, 0% packet loss
rtt min/avg/max/mdev = 22.8/23.1/24.0/0.4 ms
```

`h1 → h4` with **Path B active** (lower, 2 × 15 ms + 2 × 1 ms access):

```
--- 10.0.0.4 ping statistics ---
15 packets transmitted, 15 received, 0% packet loss
rtt min/avg/max/mdev = 62.9/63.2/64.1/0.4 ms
```

The tool prints a side-by-side comparison automatically:

```
================================================================
Delay Measurement Comparison
================================================================
label              loss %      min      avg      max     mdev
----------------------------------------------------------------
path_A_h1_h4          0.0   22.800   23.100   24.000    0.400
path_B_h1_h4          0.0   62.900   63.200   64.100    0.400
================================================================
Baseline (fastest avg): [path_A_h1_h4] 23.100 ms
  [path_B_h1_h4] is +40.100 ms (+173.6%) vs baseline
```

### 7.2 Expected output — link failure

```
before_failure      23.1 ms avg,  0% loss     (path A)
during_failure         -             100% loss (s1-s2 down, no fallback)
after_failover      63.2 ms avg,  0% loss     (ovs-ofctl swap to path B)
```

### 7.3 Expected output — firewall

```
allowed_h2_h3    0% loss, ~2.0 ms avg
blocked_h2_h4  100% loss   (priority-500 drop rule)
```

---

## 8. Performance Observation & Analysis

Once a run finishes, CSV + JSON files are under `results/`:

```
results/
├── scenario_1_baseline.csv
├── scenario_1_baseline.json
├── scenario_2b_path_compare.csv
├── scenario_2b_path_compare.json
├── scenario_3_link_failure.csv
├── scenario_3_link_failure.json
├── scenario_4_blocked.csv
└── scenario_4_blocked.json
```

Each CSV row contains:

```
label, src, dst, transmitted, received, loss_percent,
rtt_min_ms, rtt_avg_ms, rtt_max_ms, rtt_mdev_ms
```

`rtt_mdev_ms` is the mean deviation (jitter) — useful for analyzing delay
*variation*, which is explicitly mentioned in the problem statement.

### Throughput (iperf) sanity check

Inside the Mininet CLI:

```
mininet> iperf h1 h4
*** Iperf: testing TCP bandwidth between h1 and h4
*** Results: ['9.2 Mbits/sec', '9.8 Mbits/sec']
```

The observed throughput is close to the 10 Mbit configured BW of the TCLink.
Changing `PATH_MODE` between A and B mainly affects *latency*, not
throughput, which matches the design of the topology.

### Wireshark

`tcpdump` / Wireshark can be attached to any veth pair to visualise the
OpenFlow channel and the ICMP traffic:

```bash
sudo tcpdump -i lo -w /tmp/of.pcap 'tcp port 6633'
sudo tcpdump -i s1-eth2 -w /tmp/pathA.pcap
```

Open `/tmp/of.pcap` in Wireshark and set the filter `openflow_v4` to inspect
the `OFPT_FLOW_MOD` / `OFPT_PACKET_IN` / `OFPT_PACKET_OUT` messages produced
by `src/controller.py`.

---

## 9. Proof of Execution

Place screenshots / logs here (relative links render on GitHub):

- Flow tables from all four switches (`scripts/show_flows.sh` output) →
  `docs/screenshots/flow_tables.png`
- `h1 ping h4` on Path A vs Path B side-by-side →
  `docs/screenshots/path_compare.png`
- `h2 ping h4` blocked vs `h2 ping h3` allowed →
  `docs/screenshots/firewall.png`
- Wireshark capture with `openflow_v4` filter →
  `docs/screenshots/wireshark_openflow.png`
- `iperf` between h1 and h4 →
  `docs/screenshots/iperf.png`
- CSV report opened in a spreadsheet →
  `docs/screenshots/csv_report.png`

> After you record your demo run, drop PNGs into `docs/screenshots/` and the
> links above become live on GitHub.

---

## 10. Validation / Regression

A lightweight regression test covers the RTT parser (the most fragile piece
of the tool — real `ping` output varies subtly across distros):

```bash
cd src && python3 -m unittest test_parser -v
```

Expected output:

```
test_parses_full_loss ... ok
test_parses_normal_output ... ok
test_parses_partial_loss ... ok
test_summary_does_not_crash_on_empty ... ok
Ran 4 tests in 0.001s
OK
```

Beyond the unit tests, every scenario in `run_tests.py` is itself a
functional regression check:

- Scenario 1 must see >0 received packets (connectivity regression).
- Scenario 2 must show `path_B.avg > path_A.avg` (path-steering regression).
- Scenario 3 must show 100% loss during the outage window (failure
  regression).
- Scenario 4 must show 100% loss for `h2 → h4` and 0% loss for `h2 → h3`
  (firewall regression).

---

## 11. Troubleshooting

| Symptom                                  | Fix                                                          |
|------------------------------------------|--------------------------------------------------------------|
| `mn -c` complains, old switches linger   | `sudo mn -c && sudo killall -q ovs-vswitchd openvswitch`     |
| Controller not listening on 6633         | Something else grabs the port: `sudo ss -ltnp \| grep 6633`  |
| `pingall` shows X (full loss)            | Controller not running, or `BLOCK_H2_TO_H4=1` blocks h2↔h4   |
| Ping RTT is 0.05 ms (too low)            | Mininet isn't using `TCLink`; re-run `src/topology.py`       |
| Ryu crashes: `cannot set 'is_timeout' attribute of immutable type 'TimeoutError'` | Python 3.10: `pip3 install --user --force-reinstall eventlet==0.33.3` |
| Ryu crashes: `AttributeError: module 'collections' has no attribute 'MutableMapping'` | `pip3 install --user --upgrade 'dnspython>=2.3.0'`           |
| Ryu crashes: `ImportError: cannot import name 'ALREADY_HANDLED' from 'eventlet.wsgi'` | See §5.2a — append `ALREADY_HANDLED = []` to eventlet/wsgi.py |
| `ryu-manager: command not found` after pip install | `export PATH="$HOME/.local/bin:$PATH"`                    |

---

## 12. References

1. Mininet project documentation — <http://mininet.org/>
2. Ryu SDN framework — <https://ryu.readthedocs.io/en/latest/>
3. Open Networking Foundation, *OpenFlow Switch Specification v1.3.5* —
   <https://opennetworking.org/wp-content/uploads/2014/10/openflow-switch-v1.3.5.pdf>
4. N. McKeown et al., *OpenFlow: Enabling Innovation in Campus Networks*,
   ACM SIGCOMM CCR, 2008.
5. Linux `iputils` `ping(8)` man page — RTT / mdev statistics format.
6. Open vSwitch `ovs-ofctl(8)` man page — flow installation and inspection.
7. Wireshark OpenFlow dissector — <https://wiki.wireshark.org/OpenFlow>.

---

## 13. License & Author

- Project: **Network Delay Measurement Tool** (Problem #15)
- Student: **Kshitij Gupta** (PES1UG24AM915)
- Course deliverable: individual SDN Mininet + OpenFlow assignment.
