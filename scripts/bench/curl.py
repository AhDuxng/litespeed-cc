"""Transfer probe built on curl --write-out.

Every transfer is a new curl process and therefore a new connection with a full
handshake (TCP + TLS, or QUIC with TLS 1.3 inside), so each sample includes
connection establishment and the sender's congestion controller always starts
from its initial window, the regime that dominates web downloads. The protocol
options of the stack under test (stacks.py) are appended to a common base, and
the body is discarded to /dev/null so that storage bandwidth is never on the
measured path.

Write-out variables kept (times in seconds from the start of the transfer):
  time_connect        TCP handshake complete (for QUIC, the QUIC handshake)
  time_appconnect     TLS or QUIC handshake complete: the handshake time
  time_starttransfer  first response byte (TTFB)
  time_total          download time
  speed_download      size_download / time_total, in bytes per second
  size_download, http_version, response_code, errormsg  validity of the sample

The CPU time of the curl process is read from wait4(2) rusage, which is exact
and independent of any sampling grid.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

WRITE_OUT_FIELDS = (
    "http_version",
    "response_code",
    "remote_ip",
    "remote_port",
    "size_download",
    "speed_download",
    "time_namelookup",
    "time_connect",
    "time_appconnect",
    "time_pretransfer",
    "time_starttransfer",
    "time_total",
    "num_connects",
    "errormsg",
)
TRACE_MARKERS = ("Connected to", "Established connection", "SSL connection using", "ALPN:", "using HTTP/")


@dataclass(frozen=True)
class Transfer:
    started_at: float
    finished_at: float
    exit_code: int
    write_out: Mapping[str, Any]
    cpu_user_s: float
    cpu_sys_s: float

    @property
    def cpu_pct(self) -> float:
        wall_s = self.finished_at - self.started_at
        return 100.0 * (self.cpu_user_s + self.cpu_sys_s) / wall_s if wall_s > 0 else 0.0


class CurlError(RuntimeError):
    pass


class Curl:
    def __init__(self, binary: Path, connect_timeout_s: float, max_time_s: float) -> None:
        self._base = [
            str(binary),
            "--insecure",
            "--silent",
            "--connect-timeout",
            f"{connect_timeout_s:g}",
            "--max-time",
            f"{max_time_s:g}",
        ]

    def download(self, url: str, protocol: Sequence[str], on_spawn: Callable[[int], None]) -> Transfer:
        argv = [*self._base, *protocol, "--output", os.devnull, "--write-out", "%{json}", url]
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as pipe:
            try:
                started_at = time.time()
                pid = os.posix_spawnp(
                    argv[0],
                    argv,
                    dict(os.environ),
                    file_actions=[
                        (os.POSIX_SPAWN_DUP2, write_fd, 1),
                        (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                    ],
                )
            finally:
                os.close(write_fd)
            on_spawn(pid)
            report = pipe.read()
        _, status, usage = os.wait4(pid, 0)
        finished_at = time.time()
        return Transfer(
            started_at=started_at,
            finished_at=finished_at,
            exit_code=os.waitstatus_to_exitcode(status),
            write_out=_select(report),
            cpu_user_s=usage.ru_utime,
            cpu_sys_s=usage.ru_stime,
        )

    def fetch(self, url: str, protocol: Sequence[str]) -> bytes:
        return self._run([*protocol, "--show-error", "--fail", url]).stdout

    def trace(self, url: str, protocol: Sequence[str]) -> list[str]:
        stderr = self._run([*protocol, "--verbose", "--fail", "--output", os.devnull, url]).stderr
        lines = stderr.decode(errors="replace").splitlines()
        return [line for line in lines if line.startswith("*") and any(m in line for m in TRACE_MARKERS)]

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess:
        result = subprocess.run([*self._base, *arguments], capture_output=True, check=False)
        if result.returncode != 0:
            raise CurlError(f"curl exit {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
        return result


def _select(report: bytes) -> dict[str, Any]:
    try:
        values = json.loads(report) if report else {}
    except json.JSONDecodeError:
        values = {}
    return {field: values.get(field) for field in WRITE_OUT_FIELDS}
