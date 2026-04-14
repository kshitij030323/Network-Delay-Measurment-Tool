# Demo Script (for the live viva)

A 5-minute walkthrough showing every rubric point. Follow in order — each
step corresponds to a screenshot you should capture.

---

## 0. Open three terminals

- **T1** – controller
- **T2** – Mininet
- **T3** – `ovs-ofctl` / `tcpdump` / Wireshark

---

## 1. Problem Understanding (1 slide / 30 s)

Say out loud:

> "The assignment is problem #15 — build an SDN tool that measures latency
> between hosts, compares RTTs across multiple paths, and analyzes delay
> variations. I built a Mininet diamond topology with two disjoint paths
> (low- vs high-latency), a Ryu OpenFlow 1.3 controller that can pin traffic
> to either path using explicit match/action flow rules, and a Python tool
> that drives `ping` and produces CSV/JSON reports."

Show `README.md` sections 1 and 3.

---

## 2. Controller Setup (60 s)

**T1**:

```bash
./scripts/start_controller.sh
```

Point out the log lines:

- `register datapath: ...` — four switches connect.
- `table-miss flow installed` — priority-0 "packet_in" rule.
- `DROP rules installed: ICMP h2<->h4` — firewall.

**Screenshot #1** – the controller log after all four switches connect.

---

## 3. Topology (30 s)

**T2**:

```bash
sudo ./scripts/start_topology.sh
```

Inside the Mininet CLI:

```text
mininet> nodes
mininet> links
mininet> net
mininet> pingall
```

**Screenshot #2** – `pingall` output (shows that everyone reaches everyone
*except* `h2 → h4` and `h4 → h2`, which is the firewall scenario).

---

## 4. Flow Tables (60 s)

**T3**:

```bash
sudo ./scripts/show_flows.sh
```

Point out on each switch:

- priority=0 table-miss → controller
- priority=10 learned L2 flows
- priority=500 drop rules for ICMP h2↔h4

**Screenshot #3** – `ovs-ofctl dump-flows` of all four switches.

---

## 5. Baseline delay (30 s)

**T2 (Mininet CLI)**:

```text
mininet> h1 ping -c 10 h4
```

Record the `rtt min/avg/max/mdev` line.

**Screenshot #4** – ping output.

---

## 6. Path Comparison (90 s)

Restart the controller with Path A, then Path B. Easier: use the automated
runner from **T3**:

```bash
sudo python3 src/run_tests.py --scenario path_compare_auto
```

Show:

- The side-by-side "Delay Measurement Comparison" table.
- The Path B average is roughly 3× the Path A average — this matches the
  topology design (5 ms vs 15 ms per inter-switch link).

**Screenshot #5** – the comparison table printed to the terminal.

---

## 7. Link Failure (60 s)

**T3**:

```bash
sudo python3 src/run_tests.py --scenario link_failure
```

Talk through:

- Before: Path A works, ~23 ms avg.
- Link `s1-s2` is brought down → 100 % packet loss.
- Controller (or `ovs-ofctl`) re-installs rules onto Path B → traffic
  resumes with ~63 ms avg.

**Screenshot #6** – the three-way comparison (before / during / after).

---

## 8. Firewall (30 s)

**T2**:

```text
mininet> h2 ping -c 5 h3     # allowed
mininet> h2 ping -c 5 h4     # blocked
```

**Screenshot #7** – allowed vs blocked side-by-side.

---

## 9. iperf + Wireshark (60 s)

**T2**:

```text
mininet> iperf h1 h4
```

**T3** (optional – attach Wireshark to `lo` with filter `openflow_v4`):

```bash
sudo tcpdump -i lo -w /tmp/of.pcap 'tcp port 6633'
```

Open in Wireshark, show `OFPT_FLOW_MOD`, `OFPT_PACKET_IN`,
`OFPT_PACKET_OUT`.

**Screenshot #8** – Wireshark with a flow_mod expanded.

---

## 10. Validation (30 s)

**T3**:

```bash
cd src && python3 -m unittest test_parser -v
```

All four tests pass. This is the "regression / validation" piece of the
rubric.

**Screenshot #9** – test runner output.

---

## Done.

Save all screenshots under `docs/screenshots/` and commit. Your README links
pick them up automatically.
