"""Time windows that align client batches with the server's TCP algorithm switches.

The server's kernel TCP congestion control alternates on a fixed grid of Unix
time (manifest tcp_cc_schedule): within every period of P seconds, switch k sets
algorithm a_k at offset o_k. The batch of a_k runs in the window
[o_k + d, o_(k+1)), where d is the window delay. The window opens d seconds after
the switch, which absorbs the residual clock offset of the two NTP-disciplined
hosts and lets the queue left by the previous batch drain, and closes at the
next switch. With P = 600, switches 240:bbr and 540:cubic and d = 60, CUBIC
batches start at minutes x0 and BBRv1 batches at minutes x5. QUIC runs in the
same windows, on the port of the instance whose fixed algorithm matches, so all
stacks of one algorithm share one window and nearly the same Starlink
conditions.

The kernel binds the algorithm when the server accepts a connection, so a TCP
transfer is attributed correctly once its handshake ends before the window
closes. may_start() admits a transfer only if that holds even when the
handshake takes the full connect timeout; a transfer may then outlast the
window without changing algorithm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from .manifest import TcpSchedule


@dataclass(frozen=True)
class Window:
    algorithm: str
    start: float
    end: float

    def may_start(self, now: float, connect_timeout_s: float) -> bool:
        return self.start <= now and now + connect_timeout_s < self.end


def windows(schedule: TcpSchedule, after: float) -> Iterator[Window]:
    switches = schedule.switches
    period = schedule.period_s
    spans = []
    for index, switch in enumerate(switches):
        following = switches[(index + 1) % len(switches)].offset_s + (period if index + 1 == len(switches) else 0)
        start, end = switch.offset_s + schedule.window_delay_s, following
        if start >= end:
            raise ValueError(f"window of {switch.algorithm} is empty: delay exceeds the gap to the next switch")
        spans.append((switch.algorithm, start, end))
    base = math.floor(after / period) * period - period
    while True:
        for algorithm, start, end in spans:
            if base + start >= after:
                yield Window(algorithm, base + start, base + end)
        base += period
