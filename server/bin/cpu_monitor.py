#!/usr/bin/env python3
"""Sender-side CPU accounting of the OpenLiteSpeed instances.

The sender must stay far from CPU saturation for throughput to reflect the
network rather than the host, and the stacks differ in where their sending work
runs. For HTTP/3 the whole transport runs in user space inside the OpenLiteSpeed
worker: lsquic packetisation, packet protection, loss recovery and the
congestion controller. For HTTP/2 over TCP only HTTP/2 framing and TLS record
encryption run in the worker; segmentation, retransmission and the congestion
controller run in the kernel, largely in softirq context, which cgroup
accounting does not charge reliably to the container. Per-instance CPU therefore
measures user-space cost, and the kernel share of TCP appears in the host-wide
system and softirq columns.

Each instance runs in its own container, hence its own cgroup v2, whose counter
cpu.stat:usage_usec is the exact cumulative CPU time of all its processes,
immune to sampling aliasing. The cgroup is resolved from the container's main
process (docker inspect, /proc/<pid>/cgroup) and resolved again after a restart.

The host is a burstable EC2 instance (t3): once its CPU credits are spent the
hypervisor caps each vCPU, which appears only as steal time. Steal is
therefore logged; any non-zero steal during a transfer invalidates it.

Every interval one row is appended on a drift-free wall-clock grid:

    ts           Unix epoch seconds (NTP-disciplined, joinable with client runs)
    host_pct     busy share of all vCPUs from /proc/stat (100 = every vCPU busy)
    softirq_pct  share of all vCPU time spent in softirq (kernel packet processing)
    steal_pct    share of vCPU time withheld by the hypervisor
    <label>_pct  delta usage_usec / delta wall time of one instance (100 = one vCPU)

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
    def _read() -> tuple[int, int, int, int]:
        jiffies = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:9]]
        return sum(jiffies), jiffies[3] + jiffies[4], jiffies[6], jiffies[7]

    def percent(self) -> tuple[Optional[float], Optional[float], Optional[float]]:
        current = self._read()
        delta = [now - last for now, last in zip(current, self._last)]
        self._last = current
        elapsed, idle, softirq, steal = delta
        if elapsed <= 0:
            return None, None, None
        return 100.0 * (1.0 - idle / elapsed), 100.0 * softirq / elapsed, 100.0 * steal / elapsed


class ContainerCpu:
    def __init__(self, label: str, container: str) -> None:
        self.label = label
        self.container = container
        self._stat: Optional[Path] = None
        self._last: Optional[tuple[float, int]] = None

    def _resolve(self) -> Optional[Path]:
        pid = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Pid}}", self.container],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not pid.isdigit() or pid == "0":
            return None
        try:
            entry = Path(f"/proc/{pid}/cgroup").read_text().splitlines()[0]
        except (OSError, IndexError):
            return None
        return CGROUP_ROOT / entry.rpartition(":")[2].lstrip("/") / "cpu.stat"

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


def parse_container(spec: str) -> ContainerCpu:
    label, sep, container = spec.partition("=")
    if not (sep and label and container):
        raise argparse.ArgumentTypeError(f"expected LABEL=CONTAINER, got {spec!r}")
    return ContainerCpu(label, container)


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
    parser = argparse.ArgumentParser(description="Log per-container and host CPU utilisation.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("containers", nargs="+", type=parse_container, metavar="LABEL=CONTAINER")
    args = parser.parse_args()

    host = HostCpu()
    for container in args.containers:
        container.percent(time.time())

    fields = ["ts", "host_pct", "softirq_pct", "steal_pct"] + [f"{c.label}_pct" for c in args.containers]
    with open_log(args.output, fields) as handle:
        writer = csv.writer(handle)
        for now in ticks(args.interval):
            busy, softirq, steal = host.percent()
            row = [f"{now:.3f}", fmt(busy), fmt(softirq), fmt(steal)]
            writer.writerow(row + [fmt(c.percent(now)) for c in args.containers])


if __name__ == "__main__":
    sys.exit(main())
