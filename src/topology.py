#!/usr/bin/env python
"""
Mininet Topology for Network Delay Measurement Tool
----------------------------------------------------
A diamond (multi-path) topology designed to showcase delay comparison
across different paths in an SDN network.

Topology:

                    [s2]
                   /    \
                  /      \
        h1 --- [s1]      [s4] --- h4
                  \      /
                   \    /
                    [s3]
                    /  \
                  h2    h3

Link characteristics (bandwidth / delay / loss):
    h1 <-> s1 : 10 Mbps, 1 ms, 0% loss      (access link)
    h2 <-> s3 : 10 Mbps, 1 ms, 0% loss      (access link)
    h3 <-> s3 : 10 Mbps, 1 ms, 0% loss      (access link)
    h4 <-> s4 : 10 Mbps, 1 ms, 0% loss      (access link)

    s1 <-> s2 : 10 Mbps, 5 ms,  0% loss     (path A - upper)
    s2 <-> s4 : 10 Mbps, 5 ms,  0% loss     (path A - upper)

    s1 <-> s3 : 10 Mbps, 15 ms, 0% loss     (path B - lower)
    s3 <-> s4 : 10 Mbps, 15 ms, 0% loss     (path B - lower)

This creates two disjoint paths between s1 and s4 with different delays,
so the tool can compare RTTs across paths and demonstrate path-based
delay variations. The shortest path (A) has ~10 ms one-way; the longer
path (B) has ~30 ms one-way.
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


class DelayTopo(Topo):
    """Diamond topology with multiple paths of different delays."""

    def build(self):
        # --- Switches (OpenFlow 1.3) ---
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')
        s4 = self.addSwitch('s4', protocols='OpenFlow13')

        # --- Hosts ---
        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        h3 = self.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
        h4 = self.addHost('h4', ip='10.0.0.4/24', mac='00:00:00:00:00:04')

        # --- Host <-> Switch access links (low delay) ---
        self.addLink(h1, s1, bw=10, delay='1ms', loss=0)
        self.addLink(h2, s3, bw=10, delay='1ms', loss=0)
        self.addLink(h3, s3, bw=10, delay='1ms', loss=0)
        self.addLink(h4, s4, bw=10, delay='1ms', loss=0)

        # --- Path A (upper): s1 - s2 - s4  (low delay) ---
        self.addLink(s1, s2, bw=10, delay='5ms',  loss=0)
        self.addLink(s2, s4, bw=10, delay='5ms',  loss=0)

        # --- Path B (lower): s1 - s3 - s4  (higher delay) ---
        self.addLink(s1, s3, bw=10, delay='15ms', loss=0)
        self.addLink(s3, s4, bw=10, delay='15ms', loss=0)


def run(controller_ip='127.0.0.1', controller_port=6633):
    """Build and launch the Mininet network."""
    topo = DelayTopo()
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        link=TCLink,
        controller=None,
        autoSetMacs=False,
        autoStaticArp=False,
    )

    info('*** Adding remote Ryu controller\n')
    net.addController(
        'c0',
        controller=RemoteController,
        ip=controller_ip,
        port=controller_port,
    )

    info('*** Starting network\n')
    net.start()

    info('\n*** Topology summary:\n')
    info('    4 switches (s1-s4) forming a diamond topology\n')
    info('    Two disjoint paths between h1 and h4:\n')
    info('      Path A: h1-s1-s2-s4-h4  (low latency,  ~10 ms one-way)\n')
    info('      Path B: h1-s1-s3-s4-h4  (high latency, ~30 ms one-way)\n')
    info('    h2 and h3 are attached to s3 for intra-switch tests\n\n')

    info('*** Running CLI\n')
    CLI(net)

    info('*** Stopping network\n')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
