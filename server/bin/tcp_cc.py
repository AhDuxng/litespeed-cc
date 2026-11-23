#!/usr/bin/env python3
"""Time-scheduled switching of the kernel TCP congestion control algorithm.

For TCP the congestion control algorithm belongs to the kernel, not to the web
server. A passively opened socket is assigned net.ipv4.tcp_congestion_control of
its network namespace when the handshake completes (tcp_ca_openreq_child), unless
the listener pinned an algorithm with setsockopt(TCP_CONGESTION), which
OpenLiteSpeed does not. Changing the default therefore affects only connections
accepted afterwards and never a transfer in progress. The OpenLiteSpeed
containers share the host network namespace, which is the one written here.

The algorithm follows a fixed grid on Unix time, which both NTP-disciplined hosts
share: within each period the switch whose offset passed most recently is in
force. With a 600 s period and switches 240:bbr and 540:cubic, BBRv1 is set at
minutes x4 and CUBIC at minutes x9, one minute before the client windows that
open at x5 and x0. The command derives the state from the clock alone and is
idempotent, so a late timer, a reboot or a manual run all converge to the
scheduled algorithm. Every invocation appends one row to the log:

    ts          Unix epoch seconds after the write
    phase_s     position of ts within the period
    scheduled   algorithm required by the grid
    previous    kernel default before the write
    effective   kernel default read back after the write

The log is the ground truth used to attribute every TCP transfer to the
algorithm that was in force when the server accepted its connection.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

CURRENT = Path("/proc/sys/net/ipv4/tcp_congestion_control")
AVAILABLE = Path("/proc/sys/net/ipv4/tcp_available_congestion_control")
FIELDS = ["ts", "phase_s", "scheduled", "previous", "effective"]


def parse_switch(spec: str) -> tuple[int, str]:
    offset, sep, algorithm = spec.partition(":")
    if not (sep and offset.isdigit() and algorithm):
        raise argparse.ArgumentTypeError(f"expected OFFSET_S:ALGORITHM, got {spec!r}")
    return int(offset), algorithm


def scheduled(now: float, period_s: int, switches: list[tuple[int, str]]) -> tuple[float, str]:
    phase = now % period_s
    passed = [switch for switch in switches if switch[0] <= phase]
    return phase, max(passed or switches)[1]


def append(log: Path, row: list[str]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    fresh = not log.exists() or log.stat().st_size == 0
    with log.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if fresh:
            writer.writerow(FIELDS)
        writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the scheduled kernel TCP congestion control.")
    parser.add_argument("--period", type=int, required=True, help="grid period in seconds")
    parser.add_argument("--switch", type=parse_switch, action="append", required=True, metavar="OFFSET_S:ALGORITHM")
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    if any(offset >= args.period for offset, _ in args.switch):
        parser.error("every switch offset must lie within the period")
    phase, algorithm = scheduled(time.time(), args.period, sorted(args.switch))
    if algorithm not in AVAILABLE.read_text().split():
        print(f"error: {algorithm} is not loaded (tcp_available_congestion_control)", file=sys.stderr)
        return 1
    previous = CURRENT.read_text().strip()
    if previous != algorithm:
        CURRENT.write_text(algorithm)
    effective = CURRENT.read_text().strip()
    now = time.time()
    append(args.log, [f"{now:.3f}", f"{now % args.period:.3f}", algorithm, previous, effective])
    return 0 if effective == algorithm else 1


if __name__ == "__main__":
    sys.exit(main())
