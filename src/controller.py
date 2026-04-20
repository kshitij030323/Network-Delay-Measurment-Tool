#!/usr/bin/env python
"""
Ryu OpenFlow 1.3 Controller for Network Delay Measurement Tool
--------------------------------------------------------------

This controller implements:
  1. Proxy ARP: the controller replies to every ARP request itself,
     using a known {IP: MAC} table. This avoids flooding ARP across
     the diamond topology, which would otherwise create a broadcast
     storm because there is no spanning tree.
  2. Static L3 forwarding: on every switch connect, the controller
     pushes a per-destination-IP flow at priority 100 so that every
     host pair has deterministic, loop-free routing.
  3. Path steering (priority 110): h1<->h4 traffic is pinned onto
     PATH_A (upper, low delay) or PATH_B (lower, high delay) by
     overriding the default forwarding at the endpoint switches.
  4. Firewall drop rules (priority 500): ICMP between h2 and h4 is
     dropped with an empty action list (OpenFlow "drop"). This is
     the "allowed vs blocked" scenario.
  5. Periodic polling (`ryu.lib.hub` thread) of flow-table and port
     statistics, satisfying the Monitoring/Logging rubric.

Run:
    ryu-manager src/controller.py

Env vars:
    PATH_MODE=A     -- default path for h1<->h4 is A (upper, ~20ms RTT)
    PATH_MODE=B     -- default path for h1<->h4 is B (lower, ~60ms RTT)
    BLOCK_H2_TO_H4  -- set to 0 to disable the h2<->h4 firewall rule

References:
  - Ryu SDN Framework: https://ryu.readthedocs.io/
  - OpenFlow 1.3 spec: https://opennetworking.org/
  - Mininet:           http://mininet.org/
"""

import os
from operator import attrgetter

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER,
)
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, arp
from ryu.lib import hub


# --------------------------------------------------------------------------- #
# Static topology knowledge (must match src/topology.py)                      #
# --------------------------------------------------------------------------- #
# Port numbering is determined by the order of addLink() calls in DelayTopo:
#   s1: 1 -> h1 , 2 -> s2 , 3 -> s3
#   s2: 1 -> s1 , 2 -> s4
#   s3: 1 -> h2 , 2 -> h3 , 3 -> s1 , 4 -> s4
#   s4: 1 -> h4 , 2 -> s2 , 3 -> s3

H1_IP, H1_MAC = '10.0.0.1', '00:00:00:00:00:01'
H2_IP, H2_MAC = '10.0.0.2', '00:00:00:00:00:02'
H3_IP, H3_MAC = '10.0.0.3', '00:00:00:00:00:03'
H4_IP, H4_MAC = '10.0.0.4', '00:00:00:00:00:04'

HOST_MAC = {
    H1_IP: H1_MAC, H2_IP: H2_MAC, H3_IP: H3_MAC, H4_IP: H4_MAC,
}

# Default per-destination forwarding for every switch.
# h1<->h4 follows PATH A (upper, low delay) by default.
DEFAULT_FWD = {
    1: {H1_IP: 1, H2_IP: 3, H3_IP: 3, H4_IP: 2},   # s1 -> h4 via s2 (path A)
    2: {H1_IP: 1, H2_IP: 1, H3_IP: 1, H4_IP: 2},
    3: {H1_IP: 3, H2_IP: 1, H3_IP: 2, H4_IP: 4},
    4: {H1_IP: 2, H2_IP: 3, H3_IP: 3, H4_IP: 1},   # s4 -> h1 via s2 (path A)
}

# Path-specific overrides for h1<->h4. Only the endpoint switches (s1, s4)
# need a different decision; the interior switches s2/s3 already forward
# correctly to h1/h4 under either path using the default table.
PATH_OVERRIDES = {
    'A': {
        1: {(H1_IP, H4_IP): 2},   # s1 -> s2
        4: {(H4_IP, H1_IP): 2},   # s4 -> s2
    },
    'B': {
        1: {(H1_IP, H4_IP): 3},   # s1 -> s3
        4: {(H4_IP, H1_IP): 3},   # s4 -> s3
    },
}

# Flow priority layout (higher wins when matches overlap).
PRIO_TABLE_MISS = 0
PRIO_DEFAULT_FWD = 100
PRIO_PATH_OVERRIDE = 110
PRIO_FIREWALL = 500


class DelayMeasurementController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}

        mode = os.environ.get('PATH_MODE', 'A').upper()
        if mode not in ('A', 'B'):
            mode = 'A'
        self.path_mode = mode
        self.block_h2_to_h4 = os.environ.get('BLOCK_H2_TO_H4', '1') == '1'

        self.logger.info('=' * 60)
        self.logger.info('Ryu Controller: Network Delay Measurement Tool')
        self.logger.info('PATH_MODE       = %s (default for h1<->h4)',
                         self.path_mode)
        self.logger.info('BLOCK_H2_TO_H4  = %s', self.block_h2_to_h4)
        self.logger.info('=' * 60)

        self.monitor_thread = hub.spawn(self._monitor)

    # ----------------------------------------------------------------- #
    # Switch registry                                                   #
    # ----------------------------------------------------------------- #
    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if dp.id not in self.datapaths:
                self.logger.info('register datapath: %016x', dp.id)
                self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            if dp.id in self.datapaths:
                self.logger.info('unregister datapath: %016x', dp.id)
                del self.datapaths[dp.id]

    # ----------------------------------------------------------------- #
    # Initial flow-table setup                                          #
    # ----------------------------------------------------------------- #
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # (a) Table-miss: send unmatched packets to the controller.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, priority=PRIO_TABLE_MISS,
                      match=match, actions=actions)
        self.logger.info('[dpid=%s] table-miss flow installed', dp.id)

        # (b) Default L3 forwarding (priority 100) for every known host.
        self._install_default_fwd(dp)

        # (c) h1<->h4 path override (priority 110) for the configured mode.
        self._install_path_override(dp, self.path_mode)

        # (d) Firewall drop rules (priority 500).
        if self.block_h2_to_h4:
            self._install_block_rule(dp)

    # ----------------------------------------------------------------- #
    # Flow-mod helpers                                                  #
    # ----------------------------------------------------------------- #
    def add_flow(self, datapath, priority, match, actions,
                 idle_timeout=0, hard_timeout=0):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        datapath.send_msg(mod)

    def add_drop_flow(self, datapath, priority, match):
        """Empty instruction list = OpenFlow drop."""
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=[],
        )
        datapath.send_msg(mod)

    def _install_default_fwd(self, dp):
        parser = dp.ofproto_parser
        table = DEFAULT_FWD.get(dp.id, {})
        for dst_ip, out_port in table.items():
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(dp, priority=PRIO_DEFAULT_FWD,
                          match=match, actions=actions)
        self.logger.info(
            '[dpid=%s] default L3 forwarding installed (%d entries)',
            dp.id, len(table),
        )

    def _install_path_override(self, dp, mode):
        parser = dp.ofproto_parser
        table = PATH_OVERRIDES[mode].get(dp.id, {})
        for (src_ip, dst_ip), out_port in table.items():
            match = parser.OFPMatch(
                eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=dst_ip,
            )
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(dp, priority=PRIO_PATH_OVERRIDE,
                          match=match, actions=actions)
        if table:
            self.logger.info(
                '[dpid=%s] PATH_%s override installed (%d entries)',
                dp.id, mode, len(table),
            )

    def _install_block_rule(self, dp):
        parser = dp.ofproto_parser
        for src_ip, dst_ip in ((H2_IP, H4_IP), (H4_IP, H2_IP)):
            match = parser.OFPMatch(
                eth_type=0x0800, ip_proto=1,
                ipv4_src=src_ip, ipv4_dst=dst_ip,
            )
            self.add_drop_flow(dp, priority=PRIO_FIREWALL, match=match)
        self.logger.info(
            '[dpid=%s] DROP rules installed: ICMP h2<->h4', dp.id,
        )

    # ----------------------------------------------------------------- #
    # Packet-in: proxy-ARP, plus logging for anything unexpected        #
    # ----------------------------------------------------------------- #
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt is not None:
            self._handle_arp(dp, in_port, eth, arp_pkt)
            return

        # IP packets should have been handled by the static rules.
        # If one reaches us it is typically a dropped firewall hit or
        # an unknown host -- log it for observability.
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is not None:
            self.logger.info(
                '[PacketIn dpid=%s] unrouted IP %s -> %s (in_port=%s)',
                dp.id, ip_pkt.src, ip_pkt.dst, in_port,
            )

    def _handle_arp(self, dp, in_port, eth, arp_pkt):
        """Proxy-ARP: build a reply on behalf of the target host."""
        if arp_pkt.opcode != arp.ARP_REQUEST:
            return
        target_ip = arp_pkt.dst_ip
        if target_ip not in HOST_MAC:
            return
        target_mac = HOST_MAC[target_ip]

        parser = dp.ofproto_parser
        ofp = dp.ofproto

        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst=eth.src,
            src=target_mac,
        ))
        reply.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=target_mac,
            src_ip=target_ip,
            dst_mac=arp_pkt.src_mac,
            dst_ip=arp_pkt.src_ip,
        ))
        reply.serialize()

        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=actions,
            data=reply.data,
        )
        dp.send_msg(out)
        self.logger.info(
            '[ARP] dpid=%s replied %s is-at %s (asked by %s/%s, in_port=%s)',
            dp.id, target_ip, target_mac,
            arp_pkt.src_ip, arp_pkt.src_mac, in_port,
        )

    # ----------------------------------------------------------------- #
    # Periodic stats polling                                            #
    # ----------------------------------------------------------------- #
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            hub.sleep(10)

    def _request_stats(self, datapath):
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPFlowStatsRequest(datapath))
        datapath.send_msg(parser.OFPPortStatsRequest(
            datapath, 0, datapath.ofproto.OFPP_ANY,
        ))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        self.logger.info(
            '[FlowStats dpid=%016x] %d flow entries',
            ev.msg.datapath.id, len(body),
        )
        for stat in sorted(
            [f for f in body if f.priority > 0],
            key=lambda f: (-f.priority,),
        )[:8]:
            self.logger.info(
                '    pri=%d pkts=%d bytes=%d match=%s',
                stat.priority, stat.packet_count, stat.byte_count,
                dict(stat.match.items()),
            )

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        body = ev.msg.body
        for stat in sorted(body, key=attrgetter('port_no'))[:6]:
            self.logger.info(
                '[PortStats dpid=%016x port=%s] rx_pkts=%d tx_pkts=%d '
                'rx_bytes=%d tx_bytes=%d',
                ev.msg.datapath.id, stat.port_no,
                stat.rx_packets, stat.tx_packets,
                stat.rx_bytes, stat.tx_bytes,
            )
