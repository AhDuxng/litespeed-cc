"""Append-only, crash-safe record sinks.

Each record is flushed as soon as it is written, so an interrupted campaign
(power loss on the Pi, a dropped SSH session) keeps every completed
measurement. Writes are serialised by a lock because the transfer loop and the
1 Hz samplers produce records concurrently. Values are stored unrounded; an
absent value is an empty CSV cell, never a zero.
"""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


class _Sink:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("w", newline="")
        self._lock = threading.Lock()

    def __enter__(self) -> _Sink:
        return self

    def __exit__(self, *_: object) -> None:
        with self._lock:
            self._handle.close()


class CsvSink(_Sink):
    def __init__(self, path: Path, fields: Sequence[str]) -> None:
        super().__init__(path)
        self._writer = csv.DictWriter(self._handle, fieldnames=list(fields), lineterminator="\n")
        self._writer.writeheader()

    def write(self, row: Mapping[str, Any]) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._handle.flush()


class JsonlSink(_Sink):
    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
