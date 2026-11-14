"""Client CPU load during transfers.

On the Pi 4 receiver, QUIC packet protection and packet processing run in user
space on a single core, so the receiver can saturate before the Starlink link
does; differences between congestion controllers are interpretable only while
it does not. Two complementary measurements are taken:

  * per transfer, the exact CPU time of the curl process from wait4(2) rusage
    (curl.py), giving its mean utilisation over the transfer (100 % = one core);
  * a periodic series of host-wide utilisation from /proc/stat (100 % = all
    cores), utilisation of the active curl process from /proc/<pid>/stat
    (100 % = one core) and SoC temperature, tagged with the active run.

The series exposes transient saturation that a per-transfer mean hides, and the
samples between transfers give the idle baseline of the host.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .host import soc_temperature_c
from .periodic import PeriodicTask
from .sinks import CsvSink

FIELDS = ("ts", "run_id", "host_cpu_pct", "curl_cpu_pct", "soc_temp_c")
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


class HostCpu:
    def __init__(self) -> None:
        self._last = self._read()

    @staticmethod
    def _read() -> tuple[int, int]:
        jiffies = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:9]]
        return sum(jiffies), jiffies[3] + jiffies[4]

    def percent(self) -> Optional[float]:
        total, idle = self._read()
        last_total, last_idle = self._last
        self._last = (total, idle)
        if total <= last_total:
            return None
        return 100.0 * (1.0 - (idle - last_idle) / (total - last_total))


def process_cpu_seconds(pid: int) -> Optional[float]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rpartition(")")[2].split()
    return (int(fields[11]) + int(fields[12])) / CLOCK_TICKS


@dataclass(frozen=True)
class _Observation:
    run_id: str
    pid: int
    ts: float
    cpu_s: float


class CpuSampler(PeriodicTask):
    def __init__(self, sink: CsvSink, interval_s: float) -> None:
        super().__init__("cpu-sampler", interval_s)
        self._sink = sink
        self._host = HostCpu()
        self._lock = threading.Lock()
        self._tracked: Optional[_Observation] = None

    def track(self, run_id: str, pid: int) -> None:
        with self._lock:
            self._tracked = _Observation(run_id, pid, time.time(), 0.0)

    def untrack(self) -> None:
        with self._lock:
            self._tracked = None

    def _curl_percent(self, now: float) -> tuple[str, Optional[float]]:
        with self._lock:
            previous = self._tracked
            if previous is None:
                return "", None
            cpu_s = process_cpu_seconds(previous.pid)
            if cpu_s is None or now <= previous.ts:
                return previous.run_id, None
            self._tracked = _Observation(previous.run_id, previous.pid, now, cpu_s)
        return previous.run_id, 100.0 * (cpu_s - previous.cpu_s) / (now - previous.ts)

    def tick(self, now: float) -> None:
        run_id, curl_pct = self._curl_percent(now)
        self._sink.write(
            {
                "ts": now,
                "run_id": run_id,
                "host_cpu_pct": self._host.percent(),
                "curl_cpu_pct": curl_pct,
                "soc_temp_c": soc_temperature_c(),
            }
        )
