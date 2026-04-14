#!/usr/bin/env python
"""
Regression tests for DelayResult.parse().

This covers the parsing of typical `ping` output (Linux iputils format).
Run with:
    python3 -m unittest src/test_parser.py
"""

import unittest

from delay_measurement import DelayResult


SAMPLE_OK = """PING 10.0.0.4 (10.0.0.4) 56(84) bytes of data.
64 bytes from 10.0.0.4: icmp_seq=1 ttl=64 time=20.3 ms
64 bytes from 10.0.0.4: icmp_seq=2 ttl=64 time=20.1 ms
64 bytes from 10.0.0.4: icmp_seq=3 ttl=64 time=19.9 ms
64 bytes from 10.0.0.4: icmp_seq=4 ttl=64 time=20.5 ms

--- 10.0.0.4 ping statistics ---
4 packets transmitted, 4 received, 0% packet loss, time 3005ms
rtt min/avg/max/mdev = 19.900/20.200/20.500/0.224 ms
"""

SAMPLE_LOSS = """PING 10.0.0.4 (10.0.0.4) 56(84) bytes of data.

--- 10.0.0.4 ping statistics ---
5 packets transmitted, 0 received, 100% packet loss, time 4098ms
"""

SAMPLE_PARTIAL = """PING 10.0.0.4 (10.0.0.4) 56(84) bytes of data.
64 bytes from 10.0.0.4: icmp_seq=1 ttl=64 time=30.2 ms
64 bytes from 10.0.0.4: icmp_seq=3 ttl=64 time=29.8 ms

--- 10.0.0.4 ping statistics ---
3 packets transmitted, 2 received, 33% packet loss, time 2010ms
rtt min/avg/max/mdev = 29.800/30.000/30.200/0.200 ms
"""


class DelayResultParseTest(unittest.TestCase):
    def test_parses_normal_output(self):
        r = DelayResult.parse(SAMPLE_OK, 'test', 'h1', '10.0.0.4', 4)
        self.assertEqual(r.transmitted, 4)
        self.assertEqual(r.received, 4)
        self.assertEqual(r.loss_percent, 0.0)
        self.assertAlmostEqual(r.rtt_min, 19.9, places=3)
        self.assertAlmostEqual(r.rtt_avg, 20.2, places=3)
        self.assertAlmostEqual(r.rtt_max, 20.5, places=3)
        self.assertEqual(len(r.rtts_ms), 4)

    def test_parses_full_loss(self):
        r = DelayResult.parse(SAMPLE_LOSS, 'test', 'h2', '10.0.0.4', 5)
        self.assertEqual(r.transmitted, 5)
        self.assertEqual(r.received, 0)
        self.assertEqual(r.loss_percent, 100.0)
        self.assertIsNone(r.rtt_avg)
        self.assertEqual(r.rtts_ms, [])

    def test_parses_partial_loss(self):
        r = DelayResult.parse(SAMPLE_PARTIAL, 'test', 'h1', '10.0.0.4', 3)
        self.assertEqual(r.transmitted, 3)
        self.assertEqual(r.received, 2)
        self.assertEqual(r.loss_percent, 33.0)
        self.assertAlmostEqual(r.rtt_min, 29.8, places=3)
        self.assertAlmostEqual(r.rtt_max, 30.2, places=3)

    def test_summary_does_not_crash_on_empty(self):
        r = DelayResult.parse('', 'empty', 'h1', '10.0.0.4', 0)
        # should not raise
        s = r.summary()
        self.assertIn('h1', s)


if __name__ == '__main__':
    unittest.main()
