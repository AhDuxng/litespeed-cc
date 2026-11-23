"""Per-cell summary of a campaign: protocol stack x congestion control x payload.

Starlink throughput samples are skewed and heavy-tailed (15 s reallocation
boundaries, obstruction outages), so location and spread are given as order
statistics: medians of handshake time and TTFB, median with P95 for download
time, the tail a user perceives, and median with P25/P75 for throughput.
Quantiles interpolate linearly between closest ranks (Hyndman-Fan type 7).
Invalid transfers are excluded from these statistics but counted in the
completion rate, never dropped silently; skipped transfers (window closed before
start) were never attempted and are reported apart.

With --server-logs, two server records are joined by time. tcp_cc.csv states
which kernel algorithm was in force at every instant: a TCP transfer is bound to
the algorithm in force when the server accepted it, approximated by the client's
started_at + time_connect, and a transfer whose algorithm differs from its
window's is counted in tcp_cc_mismatch_runs and excluded from the statistics.
cpu.csv gives the sender's CPU time per transfer by integrating its container's
utilisation over the samples that cover the transfer, the idle gap between
transfers keeping neighbours out; server_steal_runs counts transfers during
which the burstable EC2 host had vCPU time withheld by the hypervisor, the
server-side analogue of Pi throttling.

Each transfer is also joined with the dish telemetry recorded around it. Dish
samples are 1 s aggregates, so the window [started_at, finished_at] is widened
by one sampling interval on both sides; a sub-second 2 MiB transfer is then
still described by the samples that bracket it. A cell reports the median PoP
latency, the mean PoP drop rate and the share of transfers that met an
obstruction: the conditions each cell was measured under. Client CPU is the
curl process mean utilisation from rusage (100 = one core).
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .host import throttled_now

SUMMARY_FIELDS = (
    "stack",
    "cc",
    "payload",
    "runs",
    "skipped",
    "valid",
    "completion_pct",
    "handshake_s_median",
    "ttfb_s_median",
    "download_time_s_median",
    "download_time_s_p95",
    "throughput_mbps_p25",
    "throughput_mbps_median",
    "throughput_mbps_p75",
    "client_cpu_pct_median",
    "server_cpu_s_median",
    "dish_latency_ms_median",
    "dish_drop_rate_mean",
    "obstructed_runs_pct",
    "throttled_runs",
    "server_steal_runs",
    "tcp_cc_mismatch_runs",
)


class Series:
    def __init__(self, points: Iterable[tuple[float, float]]) -> None:
        ordered = sorted(points)
        self._ts = [ts for ts, _ in ordered]
        self._values = [value for _, value in ordered]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, str]], column: str) -> Series:
        return cls((float(row["ts"]), value) for row in rows if (value := _number(row.get(column))) is not None)

    def window(self, start: float, end: float) -> list[float]:
        return self._values[bisect_left(self._ts, start) : bisect_right(self._ts, end)]

    def period(self) -> Optional[float]:
        steps = [later - earlier for earlier, later in zip(self._ts, self._ts[1:])]
        return statistics.median(steps) if steps else None


class Timeline:
    def __init__(self, rows: Iterable[Mapping[str, str]], column: str) -> None:
        ordered = sorted((float(row["ts"]), row[column]) for row in rows if row.get(column))
        self._ts = [ts for ts, _ in ordered]
        self._states = [state for _, state in ordered]

    def __bool__(self) -> bool:
        return bool(self._ts)

    def at(self, ts: float) -> Optional[str]:
        index = bisect_right(self._ts, ts)
        return self._states[index - 1] if index else None


def _number(text: Optional[str]) -> Optional[float]:
    if text in ("True", "False"):
        return float(text == "True")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _mean(values: Sequence[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def _column(rows: Iterable[Mapping[str, str]], name: str) -> list[float]:
    return [value for row in rows if (value := _number(row.get(name))) is not None]


def _per_run(
    windows: Sequence[tuple[float, float]], series: Series, reduce: Callable[[list[float]], float]
) -> list[float]:
    return [reduce(samples) for start, end in windows if (samples := series.window(start, end))]


def _tcp_cc_mismatch(run: Mapping[str, str], timeline: Timeline) -> bool:
    connect = _number(run.get("time_connect"))
    if run["transport"] != "tcp" or not timeline or not connect:
        return False
    return timeline.at(float(run["started_at"]) + connect) != run["cc"]


def summarize(results_dir: Path, server_logs: Optional[Path] = None) -> list[dict[str, Any]]:
    meta = json.loads((results_dir / "meta.json").read_text())
    margin = float(meta["config"]["dish_interval_s"])
    dish_rows = _read_csv(results_dir / "dish.csv")
    latency = Series.from_rows(dish_rows, "pop_ping_latency_ms")
    drop_rate = Series.from_rows(dish_rows, "pop_ping_drop_rate")
    obstructed = Series.from_rows(dish_rows, "currently_obstructed")
    server_rows = _read_csv(server_logs / "cpu.csv") if server_logs else []
    server_interval = Series.from_rows(server_rows, "host_pct").period() or 1.0
    steal = Series.from_rows(server_rows, "steal_pct")
    tcp_cc = Timeline(_read_csv(server_logs / "tcp_cc.csv") if server_logs else [], "effective")

    cells: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for run in _read_csv(results_dir / "runs.csv"):
        cells[(run["stack"], run["cc"], run["payload"])].append(run)

    summary = []
    for (stack, cc, payload), rows in sorted(cells.items()):
        runs = [run for run in rows if run["exit_code"] != ""]
        mismatched = [run for run in runs if _tcp_cc_mismatch(run, tcp_cc)]
        valid = [run for run in runs if run["valid"] == "1" and not _tcp_cc_mismatch(run, tcp_cc)]
        spans = [(float(run["started_at"]), float(run["finished_at"])) for run in runs]
        widened = [(start - margin, end + margin) for start, end in spans]
        covering = [(start, end + server_interval) for start, end in spans]
        server_pct = Series.from_rows(server_rows, f"{cc}_pct")
        obstruction = _per_run(widened, obstructed, max)
        summary.append(
            {
                "stack": stack,
                "cc": cc,
                "payload": payload,
                "runs": len(runs),
                "skipped": len(rows) - len(runs),
                "valid": len(valid),
                "completion_pct": 100.0 * len(valid) / len(runs) if runs else None,
                "handshake_s_median": quantile(_column(valid, "handshake_s"), 0.50),
                "ttfb_s_median": quantile(_column(valid, "ttfb_s"), 0.50),
                "download_time_s_median": quantile(_column(valid, "download_time_s"), 0.50),
                "download_time_s_p95": quantile(_column(valid, "download_time_s"), 0.95),
                "throughput_mbps_p25": quantile(_column(valid, "throughput_mbps"), 0.25),
                "throughput_mbps_median": quantile(_column(valid, "throughput_mbps"), 0.50),
                "throughput_mbps_p75": quantile(_column(valid, "throughput_mbps"), 0.75),
                "client_cpu_pct_median": quantile(_column(valid, "cpu_pct"), 0.50),
                "server_cpu_s_median": quantile(
                    _per_run(covering, server_pct, lambda pct: sum(pct) / 100.0 * server_interval), 0.50
                ),
                "dish_latency_ms_median": quantile(_per_run(widened, latency, statistics.median), 0.50),
                "dish_drop_rate_mean": _mean(_per_run(widened, drop_rate, statistics.fmean)),
                "obstructed_runs_pct": 100.0 * sum(obstruction) / len(obstruction) if obstruction else None,
                "throttled_runs": sum(
                    throttled_now(run["throttled_before"]) or throttled_now(run["throttled_after"]) for run in runs
                ),
                "server_steal_runs": sum(peak > 0 for peak in _per_run(covering, steal, max)) if server_rows else None,
                "tcp_cc_mismatch_runs": len(mismatched) if tcp_cc else None,
            }
        )
    return summary


def write(summary: Sequence[Mapping[str, Any]], path: Path) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(summary)


def render(summary: Sequence[Mapping[str, Any]]) -> str:
    header = (
        f"{'stack':<10}{'cc':<7}{'payload':<10}{'valid':>8}{'hs s':>8}{'ttfb s':>8}"
        f"{'time med/p95 s':>18}{'Mbps p25/med/p75':>26}{'cpu%':>7}{'PoP ms':>8}{'obstr%':>8}"
    )
    lines = [header, "-" * len(header)]
    for cell in summary:
        lines.append(
            f"{cell['stack']:<10}{cell['cc']:<7}{cell['payload']:<10}{cell['valid']:>4}/{cell['runs']:<3}"
            f"{_fmt(cell['handshake_s_median'], 3):>8}{_fmt(cell['ttfb_s_median'], 3):>8}"
            f"{_fmt(cell['download_time_s_median'], 3):>9}{_fmt(cell['download_time_s_p95'], 3):>9}"
            f"{_fmt(cell['throughput_mbps_p25']):>9}{_fmt(cell['throughput_mbps_median']):>9}"
            f"{_fmt(cell['throughput_mbps_p75']):>8}"
            f"{_fmt(cell['client_cpu_pct_median'], 1):>7}{_fmt(cell['dish_latency_ms_median'], 1):>8}"
            f"{_fmt(cell['obstructed_runs_pct'], 0):>8}"
        )
    return "\n".join(lines)


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"
