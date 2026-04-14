#!/usr/bin/env python
"""
Network Delay Measurement Tool
------------------------------
Runs `ping` between two hosts (optionally inside a Mininet context), parses
the output, and produces:
  * per-packet RTT list
  * min / avg / max / mdev (jitter) / loss %
  * comparison across multiple runs (paths)
  * CSV + JSON reports under results/

Can be used two ways:

    1. Standalone (on the real host or inside an `h1 python ...` Mininet
       CLI invocation):
           python3 src/delay_measurement.py 10.0.0.4 -c 20

    2. Driven by run_tests.py which calls host.cmd('ping ...') inside
       the Mininet Python API and feeds the stdout to DelayResult.parse().

All math is done with the standard library -- no external deps.
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional


RTT_LINE_RE = re.compile(
    r'time[=<]\s*([0-9]+\.?[0-9]*)\s*ms', re.IGNORECASE)
LOSS_RE = re.compile(
    r'(\d+)\s+packets transmitted,\s+(\d+)\s+received,'
    r'.*?(\d+(?:\.\d+)?)%\s+packet loss', re.DOTALL)
RTT_SUMMARY_RE = re.compile(
    r'rtt min/avg/max/mdev\s*=\s*'
    r'([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)\s*ms')


@dataclass
class DelayResult:
    """Parsed result of a single ping run."""
    label: str
    src: str
    dst: str
    count: int
    rtts_ms: List[float] = field(default_factory=list)
    transmitted: int = 0
    received: int = 0
    loss_percent: float = 0.0
    rtt_min: Optional[float] = None
    rtt_avg: Optional[float] = None
    rtt_max: Optional[float] = None
    rtt_mdev: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    raw: str = ''

    # ---- parsing ----------------------------------------------------------
    @classmethod
    def parse(cls, output: str, label: str, src: str, dst: str,
              count: int) -> 'DelayResult':
        r = cls(label=label, src=src, dst=dst, count=count, raw=output)

        r.rtts_ms = [float(m.group(1)) for m in RTT_LINE_RE.finditer(output)]

        loss_m = LOSS_RE.search(output)
        if loss_m:
            r.transmitted = int(loss_m.group(1))
            r.received = int(loss_m.group(2))
            r.loss_percent = float(loss_m.group(3))

        sum_m = RTT_SUMMARY_RE.search(output)
        if sum_m:
            r.rtt_min = float(sum_m.group(1))
            r.rtt_avg = float(sum_m.group(2))
            r.rtt_max = float(sum_m.group(3))
            r.rtt_mdev = float(sum_m.group(4))
        elif r.rtts_ms:
            # Compute ourselves if ping output didn't include the summary
            r.rtt_min = min(r.rtts_ms)
            r.rtt_max = max(r.rtts_ms)
            r.rtt_avg = statistics.fmean(r.rtts_ms)
            r.rtt_mdev = (
                statistics.pstdev(r.rtts_ms) if len(r.rtts_ms) > 1 else 0.0
            )
        return r

    # ---- convenience ------------------------------------------------------
    def summary(self) -> str:
        if self.rtt_avg is None:
            return f'[{self.label}] no RTT samples ({self.src}->{self.dst})'
        return (
            f'[{self.label}] {self.src} -> {self.dst}  '
            f'n={self.received}/{self.transmitted} '
            f'loss={self.loss_percent:.1f}%  '
            f'min/avg/max/mdev = '
            f'{self.rtt_min:.3f}/{self.rtt_avg:.3f}/'
            f'{self.rtt_max:.3f}/{self.rtt_mdev:.3f} ms'
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop('raw', None)
        return d


def run_ping(dst: str, count: int = 10, interval: float = 0.5,
             timeout: int = 2, src_label: str = 'local') -> DelayResult:
    """Run the system ping command and parse its output."""
    cmd = [
        'ping', '-c', str(count),
        '-i', str(interval),
        '-W', str(timeout),
        dst,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=count * (interval + timeout) + 10)
        output = proc.stdout + '\n' + proc.stderr
    except FileNotFoundError:
        raise RuntimeError("`ping` binary not found in PATH")
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or '') + '\n' + (e.stderr or '')

    return DelayResult.parse(output, label=src_label, src=src_label,
                             dst=dst, count=count)


# -----------------------------------------------------------------------------
# Report writers
# -----------------------------------------------------------------------------
def save_results(results: List[DelayResult], results_dir: str,
                 run_name: str) -> None:
    os.makedirs(results_dir, exist_ok=True)
    json_path = os.path.join(results_dir, f'{run_name}.json')
    csv_path = os.path.join(results_dir, f'{run_name}.csv')

    with open(json_path, 'w') as f:
        json.dump([r.to_dict() for r in results], f, indent=2)

    with open(csv_path, 'w') as f:
        f.write('label,src,dst,transmitted,received,loss_percent,'
                'rtt_min_ms,rtt_avg_ms,rtt_max_ms,rtt_mdev_ms\n')
        for r in results:
            f.write(
                f'{r.label},{r.src},{r.dst},{r.transmitted},{r.received},'
                f'{r.loss_percent:.3f},'
                f'{r.rtt_min or 0:.3f},{r.rtt_avg or 0:.3f},'
                f'{r.rtt_max or 0:.3f},{r.rtt_mdev or 0:.3f}\n'
            )
    print(f'[+] saved {json_path}')
    print(f'[+] saved {csv_path}')


def print_comparison(results: List[DelayResult]) -> None:
    """Pretty side-by-side comparison of multiple runs."""
    if not results:
        return
    print()
    print('=' * 78)
    print('Delay Measurement Comparison')
    print('=' * 78)
    header = f'{"label":<18} {"loss %":>8} {"min":>8} {"avg":>8} {"max":>8} {"mdev":>8}'
    print(header)
    print('-' * 78)
    for r in results:
        if r.rtt_avg is None:
            print(f'{r.label:<18} {r.loss_percent:>8.1f} {"-":>8} {"-":>8} {"-":>8} {"-":>8}')
        else:
            print(
                f'{r.label:<18} {r.loss_percent:>8.1f} '
                f'{r.rtt_min:>8.3f} {r.rtt_avg:>8.3f} '
                f'{r.rtt_max:>8.3f} {r.rtt_mdev:>8.3f}'
            )
    print('=' * 78)

    # Relative comparison to the fastest avg
    valid = [r for r in results if r.rtt_avg is not None]
    if len(valid) >= 2:
        fastest = min(valid, key=lambda r: r.rtt_avg)
        print(f'Baseline (fastest avg): [{fastest.label}] {fastest.rtt_avg:.3f} ms')
        for r in valid:
            if r is fastest:
                continue
            delta = r.rtt_avg - fastest.rtt_avg
            pct = (delta / fastest.rtt_avg) * 100.0
            print(f'  [{r.label}] is +{delta:.3f} ms ({pct:+.1f}%) vs baseline')
        print('=' * 78)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description='SDN Network Delay Measurement Tool (ping-based)')
    p.add_argument('destinations', nargs='+',
                   help='one or more destination IPs')
    p.add_argument('-c', '--count', type=int, default=10,
                   help='ping count per destination (default 10)')
    p.add_argument('-i', '--interval', type=float, default=0.5,
                   help='interval between pings in seconds (default 0.5)')
    p.add_argument('-W', '--wait', type=int, default=2,
                   help='per-ping timeout in seconds (default 2)')
    p.add_argument('--label', default='run',
                   help='label prefix used in report filenames')
    p.add_argument('--results-dir', default='results',
                   help='directory for CSV/JSON reports (default ./results)')
    args = p.parse_args(argv)

    results = []
    for dst in args.destinations:
        print(f'\n[*] Pinging {dst} ({args.count} packets, interval={args.interval}s)...')
        r = run_ping(dst, count=args.count, interval=args.interval,
                     timeout=args.wait, src_label=f'{args.label}->{dst}')
        print(r.summary())
        results.append(r)

    print_comparison(results)
    save_results(results, args.results_dir, args.label)
    return 0


if __name__ == '__main__':
    sys.exit(main())
