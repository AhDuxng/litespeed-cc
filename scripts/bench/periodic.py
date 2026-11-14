"""Drift-free periodic sampling shared by the 1 Hz telemetry sources.

Sleeping a fixed period after each sample adds the sample's own latency (a gRPC
round trip, /proc reads) to every period, so the grid slowly drifts and series
from different sources fall out of phase. Deadlines are instead advanced on the
monotonic clock, and a slot that is overrun is skipped rather than compressed:
samples stay on a fixed grid, and every gap in the data is a real gap. Each
sample is stamped with wall-clock time so it can be joined with transfer
intervals and with the server's logs.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Iterator


def ticks(interval_s: float, stop: threading.Event) -> Iterator[float]:
    deadline = time.monotonic()
    while not stop.is_set():
        yield time.time()
        deadline += interval_s
        now = time.monotonic()
        if now > deadline:
            deadline += math.ceil((now - deadline) / interval_s) * interval_s
        if stop.wait(deadline - now):
            return


class PeriodicTask:
    def __init__(self, name: str, interval_s: float) -> None:
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)

    def __enter__(self) -> PeriodicTask:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        try:
            for now in ticks(self._interval_s, self._stop):
                self.tick(now)
        finally:
            self.close()

    def tick(self, now: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass
