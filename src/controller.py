#!/usr/bin/env python
"""
Ryu OpenFlow 1.3 Controller for Network Delay Measurement Tool
--------------------------------------------------------------

This controller implements:
  1. A learning-switch baseline for generic reachability.
  2. Explicit path-steering flow rules between h1 (10.0.0.1) and
     h4 (10.0.0.4), so the measurement tool can force traffic onto
     PATH_A (low delay) or PATH_B (high delay) on demand.
  3. An ACL / firewall rule that blocks ICMP from h2 -> h4 to
     demonstrate the "allowed vs blocked" test scenario.
  4. Periodic polling of flow-table and port statistics for the
     "monitoring / logging" evaluation component.

Run:
    ryu-manager src/controller.py

REST-like switching between Path A / Path B is done by restarting
the controller with different MODE env vars, e.g.:
    PATH_MODE=A ryu-manager src/controller.py   # use upper path
    PATH_MODE=B ryu-manager src/controller.py   # use lower path
    PATH_MODE=LEARN ryu-manager src/controller.py  # default: L2 learn

References (cited in README):
  - Ryu SDN Framework docs: https://ryu.readthedocs.io/
  - OpenFlow 1.3 spec (ONF)
  - Mininet: http://mininet.org/
"""

import os
from operator import attrgetter

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, icmp, arp
from ryu.lib import hub


# -----------------------------------------------------------------------------
# Static topology knowledge (matches src/topology.py)
# -----------------------------------------------------------------------------
# Port numbering below is the expected OFPort for each switch after the
# links are added by topology.py. It assumes links are added in the order
# defined in DelayTopo.build(). We re-derive the mapping here to avoid
# relying on private Mininet internals.
#
# s1 ports:  1 -> h1,   2 -> s2,   3 -> s3
# s2 ports:  1 -> s1,   2 -> s4
# s3 ports:  1 -> h2,   2 -> h3,   3 -> s1,   4 -> s4
# s4 ports:  1 -> h4,   2 -> s2,   3 -> s3
# -----------------------------------------------------------------------------

H1_IP = '10.0.0.1'
H2_IP = '10.0.0.2'
H3_IP = '10.0.0.3'
H4_IP = '10.0.0.4'

# Path routes as {dpid: out_port} for each direction
PATH_A = {
    'h1_to_h4': {1: 2, 2: 2, 4: 1},    # s1->s2, s2->s4, s4->h4
    'h4_to_h1': {4: 2, 2: 1, 1: 1},    # s4->s2, s2->s1, s1->h1
}
PATH_B = {
    'h1_to_h4': {1: 3, 3: 4, 4: 1},    # s1->s3, s3->s4, s4->h4
    'h4_to_h1': {4: 3, 3: 3, 1: 1},    # s4->s3, s3->s1, s1->h1
}


class DelayMeasurementController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # mac_to_port[dpid][mac] = port   --- for the L2 learning fallback
        self.mac_to_port = {}
        # datapaths[dpid] = datapath      --- for stats polling
        self.datapaths = {}
        self.path_mode = os.environ.get('PATH_MODE', 'LEARN').upper()
        self.block_h2_to_h4 = os.environ.get('BLOCK_H2_TO_H4', '1') == '1'

        self.logger.info('=' * 60)
        self.logger.info('Ryu Controller: Network Delay Measurement Tool')
        self.logger.info('PATH_MODE       = %s', self.path_mode)
        self.logger.info('BLOCK_H2_TO_H4  = %s', self.block_h2_to_h4)
        self.logger.info('=' * 60)

        # Spawn the stats-polling thread
        self.monitor_thread = hub.spawn(self._monitor)

    # -------------------------------------------------------------------------
    # Switch connect / disconnect
    # -------------------------------------------------------------------------
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

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss: send unmatched packets to controller.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, priority=0, match=match, actions=actions)
        self.logger.info('[dpid=%s] table-miss flow installed', datapath.id)

        # Pre-install explicit routes for the configured mode.
        if self.path_mode in ('A', 'B'):
            self._install_static_path(datapath, self.path_mode)

        # Firewall: block ICMP from h2 -> h4 (and return path) if enabled.
        if self.block_h2_to_h4:
            self._install_block_rule(datapath)

    # -------------------------------------------------------------------------
    # Flow utilities
    # -------------------------------------------------------------------------
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
        """A flow with no actions drops matched packets."""
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=[],   # empty action list == drop
        )
        datapath.send_msg(mod)

    def _install_static_path(self, datapath, mode):
        """Install explicit per-switch flows for h1<->h4 on path A or B."""
        parser = datapath.ofproto_parser
        path = PATH_A if mode == 'A' else PATH_B
        dpid = datapath.id

        # h1 -> h4 (match on IPv4 src/dst)
        if dpid in path['h1_to_h4']:
            out_port = path['h1_to_h4'][dpid]
            match = parser.OFPMatch(
                eth_type=0x0800, ipv4_src=H1_IP, ipv4_dst=H4_IP)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, priority=100, match=match, actions=actions)

            # And the ICMP version to be explicit (some kernels need it)
            match = parser.OFPMatch(
                eth_type=0x0800, ip_proto=1,
                ipv4_src=H1_IP, ipv4_dst=H4_IP)
            self.add_flow(datapath, priority=110, match=match, actions=actions)

        # h4 -> h1 (return path)
        if dpid in path['h4_to_h1']:
            out_port = path['h4_to_h1'][dpid]
            match = parser.OFPMatch(
                eth_type=0x0800, ipv4_src=H4_IP, ipv4_dst=H1_IP)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, priority=100, match=match, actions=actions)

            match = parser.OFPMatch(
                eth_type=0x0800, ip_proto=1,
                ipv4_src=H4_IP, ipv4_dst=H1_IP)
            self.add_flow(datapath, priority=110, match=match, actions=actions)

        self.logger.info(
            '[dpid=%s] static PATH_%s rules installed for h1<->h4',
            dpid, mode,
        )

    def _install_block_rule(self, datapath):
        """Drop ICMP between h2 and h4 at every switch (firewall demo)."""
        parser = datapath.ofproto_parser
        # h2 -> h4
        match = parser.OFPMatch(
            eth_type=0x0800, ip_proto=1,
            ipv4_src=H2_IP, ipv4_dst=H4_IP)
        self.add_drop_flow(datapath, priority=500, match=match)
        # h4 -> h2
        match = parser.OFPMatch(
            eth_type=0x0800, ip_proto=1,
            ipv4_src=H4_IP, ipv4_dst=H2_IP)
        self.add_drop_flow(datapath, priority=500, match=match)
        self.logger.info(
            '[dpid=%s] DROP rules installed: ICMP h2<->h4', datapath.id)

    # -------------------------------------------------------------------------
    # Packet-in: L2 learning-switch fallback for everything not covered above
    # -------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return  # ignore LLDP / non-ethernet

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        src = eth.src
        dst = eth.dst

        # Learn src -> in_port
        self.mac_to_port[dpid][src] = in_port

        # Decide out_port
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofp.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # Install a flow so the switch doesn't keep bouncing packets
        # to the controller for this (dpid, dst) pair.
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            self.add_flow(datapath, priority=10, match=match, actions=actions,
                          idle_timeout=30, hard_timeout=120)

        # Send the triggering packet back out.
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data,
        )
        datapath.send_msg(out)

        # Log interesting traffic (ICMP) for visibility
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt and pkt.get_protocol(icmp.icmp):
            self.logger.info(
                '[PacketIn] dpid=%s ICMP %s -> %s (in_port=%s, out_port=%s)',
                dpid, ip_pkt.src, ip_pkt.dst, in_port, out_port,
            )

    # -------------------------------------------------------------------------
    # Stats monitoring (for "monitoring/logging" evaluation component)
    # -------------------------------------------------------------------------
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            hub.sleep(10)

    def _request_stats(self, datapath):
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPFlowStatsRequest(datapath))
        datapath.send_msg(
            parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        self.logger.info(
            '[FlowStats dpid=%016x] %d flow entries',
            ev.msg.datapath.id, len(body))
        for stat in sorted(
            [f for f in body if f.priority > 0],
            key=lambda f: (-f.priority, f.match.get('in_port', 0)),
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
