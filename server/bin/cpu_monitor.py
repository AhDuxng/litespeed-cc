#!/usr/bin/env python3
"""Sender-side CPU accounting of the picoquic instances.

Congestion controllers differ in per-ACK work: BBRv3 maintains windowed
bandwidth and RTT filters and a pacing model, CUBIC evaluates its cubic window
function with HyStart, NewReno performs additive increase. The sender must also
stay far from CPU saturation for throughput to reflect the network rather than
the host.

Interpretation caveat: picoquicdemo waits in select() with a timeout equal to
the next pacing deadline, typically tens of microseconds while a flow is paced.
Over a real RTT path the loop therefore wakes continuously and its CPU time
grows with transfer duration rather than with bytes sent (on this host a 50 MiB
loopback transfer costs 0.17 CPU-s at 2.4 Gbit/s, the same payload over
Starlink about 4.5 CPU-s at 85 Mbit/s). High per-instance utilisation during a
transfer is polling, not exhausted capacity.

The host is a burstable EC2 instance (t3): once its CPU credits are spent the
hypervisor caps each vCPU, which appears only as steal time. Steal is
therefore logged; any non-zero steal during a transfer invalidates it.

Every instance runs in its own systemd unit, hence its own cgroup v2, whose
counter cpu.stat:usage_usec is the exact cumulative CPU time of all its threads,
immune to sampling aliasing and to process restarts. Every interval one row is
appended on a drift-free wall-clock grid:

    ts           Unix epoch seconds (NTP-disciplined, joinable with client runs)
    host_pct     busy share of all vCPUs from /proc/stat (100 = every vCPU busy)
    steal_pct    share of vCPU time withheld by the hypervisor
    <label>_pct  delta usage_usec / delta wall time of one unit (100 = one vCPU)

A log written with a different column set is renamed, never appended to.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional, TextIO

CGROUP_ROOT = Path("/sys/fs/cgroup")


class HostCpu:
    def __init__(self) -> None:
        self._last = self._read()

    @staticmethod
    def _read() -> tuple[int, int, int]:
        jiffies = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:9]]
        return sum(jiffies), jiffies[3] + jiffies[4], jiffies[7]

    def percent(self) -> tuple[Optional[float], Optional[float]]:
        total, idle, steal = self._read()
        last_total, last_idle, last_steal = self._last
        self._last = (total, idle, steal)
        elapsed = total - last_total
        if elapsed <= 0:
            return None, None
        return 100.0 * (1.0 - (idle - last_idle) / elapsed), 100.0 * (steal - last_steal) / elapsed


class UnitCpu:
    def __init__(self, label: str, unit: str) -> None:
        self.label = label
        self.unit = unit
        self._stat: Optional[Path] = None
        self._last: Optional[tuple[float, int]] = None

    def _resolve(self) -> Optional[Path]:
        group = subprocess.run(
            ["systemctl", "show", "--property=ControlGroup", "--value", self.unit],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return CGROUP_ROOT / group.lstrip("/") / "cpu.stat" if group else None

    def _usage_usec(self) -> Optional[int]:
        if self._stat is None or not self._stat.exists():
            self._stat = self._resolve()
        if self._stat is None:
            return None
        try:
            for line in self._stat.read_text().splitlines():
                key, _, value = line.partition(" ")
                if key == "usage_usec":
                    return int(value)
        except FileNotFoundError:
            self._stat = None
        return None

    def percent(self, now: float) -> Optional[float]:
        usage = self._usage_usec()
        previous = self._last
        self._last = None if usage is None else (now, usage)
        if usage is None or previous is None or usage < previous[1]:
            return None
        return 100.0 * (usage - previous[1]) / 1e6 / (now - previous[0])


def parse_unit(spec: str) -> UnitCpu:
    label, sep, unit = spec.partition("=")
    if not (sep and label and unit):
        raise argparse.ArgumentTypeError(f"expected LABEL=UNIT, got {spec!r}")
    return UnitCpu(label, unit)


def ticks(interval: float) -> Iterator[float]:
    deadline = time.monotonic()
    while True:
        deadline = max(deadline + interval, time.monotonic())
        time.sleep(deadline - time.monotonic())
        yield time.time()


def fmt(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def open_log(path: Path, fields: list[str]) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        with path.open(newline="") as existing:
            if next(csv.reader(existing), None) == fields:
                return path.open("a", newline="", buffering=1)
        path.rename(path.with_name(f"{path.stem}.{int(time.time())}{path.suffix}"))
    handle = path.open("w", newline="", buffering=1)
    csv.writer(handle).writerow(fields)
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(description="Log per-unit and host CPU utilisation.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("units", nargs="+", type=parse_unit, metavar="LABEL=UNIT")
    args = parser.parse_args()

    host = HostCpu()
    for unit in args.units:
        unit.percent(time.time())

    fields = ["ts", "host_pct", "steal_pct"] + [f"{u.label}_pct" for u in args.units]
    with open_log(args.output, fields) as handle:
        writer = csv.writer(handle)
        for now in ticks(args.interval):
            busy, steal = host.percent()
            writer.writerow([f"{now:.3f}", fmt(busy), fmt(steal)] + [fmt(u.percent(now)) for u in args.units])


if __name__ == "__main__":
    sys.exit(main())
