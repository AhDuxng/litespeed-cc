"""Measurement campaign: protocol stack x server congestion control x payload over Starlink.

Design
  Factors   protocol stack (HTTP/2 over TCP+TLS 1.2, HTTP/2 over TCP+TLS 1.3,
            HTTP/3 over QUIC) x congestion control algorithm of the
            OpenLiteSpeed sender (CUBIC, BBRv1) x payload size (2 MiB, dominated
            by handshake and slow start / startup; 50 MiB, dominated by the
            steady state and loss recovery): 12 cells.
  Algorithm For QUIC the algorithm is fixed per server port; for TCP it is the
            server's kernel default, switched on a grid of Unix time
            (schedule.py). Each algorithm therefore owns recurring windows, and
            in each window the client runs one batch: every stack x payload
            once, in random order (seed recorded). HTTP/3 goes to the udp port
            of the window's instance; HTTP/2 goes to the single tcp listener,
            whose accepted sockets take the kernel algorithm of the window. The
            instance that served a transfer is recorded as its server, the
            container whose CPU it consumed. A round is one window per
            algorithm.
  Blocking  The transfers of a batch fall within about a minute and meet nearly
            the same Starlink conditions, which drift with the 15 s satellite
            reallocation cycle, beam load, weather and obstruction. Stacks are
            compared inside a window, algorithms between adjacent windows;
            randomising the order inside each batch spreads the residual drift
            evenly over the cells instead of confounding it with one of them.
  Spacing   an idle gap precedes every transfer so that queues built by one
            transfer drain before the next starts, and so that dish and CPU
            telemetry also capture the idle baseline.
  Window    a transfer starts only if its handshake can complete before the
            next switch, whatever its duration; otherwise it is recorded as
            skipped (empty exit_code) and never retried.
  Validity  a sample is valid only if curl exits 0, the status is 200, the
            negotiated HTTP version is the stack's and the byte count equals
            the manifest size. Invalid samples are kept and flagged: completion
            rate is a result.

Outputs (results/<UTC start>/)
  meta.json              configuration, server manifest, stacks, verbose
                         connection traces per stack and port, host provenance
  runs.csv               one row per transfer: curl write-out, CPU time, throttling
  cpu.csv                periodic client CPU and SoC temperature, tagged by run
  dish.csv, dish.jsonl   1 Hz Starlink dish status (projection and raw message)
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import cpu, dish, host
from .config import Config
from .curl import WRITE_OUT_FIELDS, Curl, Transfer
from .manifest import MANIFEST_PATH, Endpoint, Instance, Manifest, Payload
from .schedule import Window, windows
from .sinks import CsvSink, JsonlSink
from .stacks import Stack, stacks

MANIFEST_STACK = "h2-tls13"
SKIPPED = "window closed before start"

RUN_FIELDS = (
    "run_id",
    "round",
    "window",
    "window_start",
    "slot",
    "stack",
    "transport",
    "cc",
    "server",
    "port",
    "payload",
    "payload_bytes",
    "started_at",
    "finished_at",
    "valid",
    "exit_code",
    "handshake_s",
    "ttfb_s",
    "download_time_s",
    "throughput_mbps",
    *WRITE_OUT_FIELDS,
    "cpu_user_s",
    "cpu_sys_s",
    "cpu_pct",
    "soc_temp_c",
    "throttled_before",
    "throttled_after",
)


@dataclass(frozen=True)
class Plan:
    out_dir: Path
    manifest: Manifest
    seed: int


@dataclass(frozen=True)
class Trial:
    round_no: int
    window_no: int
    window: Window
    slot: int
    stack: Stack
    instance: Instance
    server: str
    endpoint: Endpoint
    payload: Payload

    @property
    def run_id(self) -> str:
        return f"{self.window_no:04d}-{self.slot}"

    def row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "round": self.round_no,
            "window": self.window_no,
            "window_start": self.window.start,
            "slot": self.slot,
            "stack": self.stack.name,
            "transport": self.stack.transport,
            "cc": self.instance.label,
            "server": self.server,
            "port": self.endpoint.port,
            "payload": self.payload.name,
            "payload_bytes": self.payload.size_bytes,
        }


class Campaign:
    def __init__(self, config: Config, with_dish: bool) -> None:
        self._config = config
        self._with_dish = with_dish
        self._curl = Curl(config.curl_bin, config.connect_timeout_s, config.max_time_s)
        self._stacks = stacks(config.tls12_ciphers, config.tls13_ciphers, config.tls_groups)

    def prepare(self) -> Plan:
        fetch_stack = next(stack for stack in self._stacks if stack.name == MANIFEST_STACK)
        origin = Endpoint(self._config.server_host, self._config.server_host, self._config.manifest_port)
        manifest = Manifest.parse(self._curl.fetch(origin.url(MANIFEST_PATH), fetch_stack.curl_args))
        next(windows(manifest.schedule, time.time()))
        traces = []
        for stack in self._stacks:
            for endpoint in dict.fromkeys(
                manifest.endpoint(self._config.server_host, instance, stack.transport) for instance in manifest.instances
            ):
                lines = self._curl.trace(endpoint.url(MANIFEST_PATH), stack.curl_args + endpoint.pin())
                traces.append({"stack": stack.name, "port": endpoint.port, "trace": lines})
        dish_status = self._probe_dish() if self._with_dish else None
        seed = self._config.seed if self._config.seed is not None else random.SystemRandom().randrange(2**32)
        out_dir = self._config.results_dir / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out_dir.mkdir(parents=True)
        meta = {
            "started_at": time.time(),
            "seed": seed,
            "with_dish": self._with_dish,
            "config": self._config.as_dict(),
            "manifest": manifest.document,
            "stacks": [dataclasses.asdict(stack) for stack in self._stacks],
            "connection_traces": traces,
            "host": host.describe(self._config.server_host, self._config.curl_bin),
            "dish_at_start": dish_status,
        }
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        return Plan(out_dir, manifest, seed)

    def run(self, plan: Plan) -> None:
        manifest = plan.manifest
        algorithms = len(manifest.schedule.switches)
        cells = list(itertools.product(self._stacks, manifest.payloads))
        window_count = self._config.rounds * algorithms
        total = window_count * len(cells)
        rng = random.Random(plan.seed)
        index = 0
        with contextlib.ExitStack() as stack:
            runs = stack.enter_context(CsvSink(plan.out_dir / "runs.csv", RUN_FIELDS))
            cpu_sink = stack.enter_context(CsvSink(plan.out_dir / "cpu.csv", cpu.FIELDS))
            sampler = stack.enter_context(cpu.CpuSampler(cpu_sink, self._config.cpu_interval_s))
            if self._with_dish:
                table = stack.enter_context(CsvSink(plan.out_dir / "dish.csv", dish.FIELDS))
                raw = stack.enter_context(JsonlSink(plan.out_dir / "dish.jsonl"))
                stack.enter_context(dish.DishPoller(self._config.dish_address, self._config.dish_interval_s, table, raw))
            upcoming = itertools.islice(windows(manifest.schedule, time.time()), window_count)
            for window_no, window in enumerate(upcoming, 1):
                print(_announce(window_no, window_count, window), flush=True)
                time.sleep(max(0.0, window.start - time.time()))
                instance = manifest.instance(window.algorithm)
                for slot, (protocol, payload) in enumerate(rng.sample(cells, len(cells)), 1):
                    time.sleep(self._config.gap_s)
                    round_no = (window_no - 1) // algorithms + 1
                    endpoint = manifest.endpoint(self._config.server_host, instance, protocol.transport)
                    server = manifest.server(instance, protocol.transport)
                    trial = Trial(round_no, window_no, window, slot, protocol, instance, server, endpoint, payload)
                    if window.may_start(time.time(), self._config.connect_timeout_s):
                        row = self._measure(sampler, trial)
                    else:
                        row = {**trial.row(), "started_at": time.time(), "valid": 0, "errormsg": SKIPPED}
                    runs.write(row)
                    index += 1
                    print(_progress(index, total, row), flush=True)

    def _measure(self, sampler: cpu.CpuSampler, trial: Trial) -> dict[str, Any]:
        throttled_before = host.throttled_flags()
        soc_temp_c = host.soc_temperature_c()
        transfer = self._curl.download(
            trial.endpoint.url(trial.payload.name),
            trial.stack.curl_args + trial.endpoint.pin(),
            on_spawn=lambda pid: sampler.track(trial.run_id, pid),
        )
        sampler.untrack()
        out = transfer.write_out
        speed = out["speed_download"]
        return {
            **trial.row(),
            "started_at": transfer.started_at,
            "finished_at": transfer.finished_at,
            "valid": int(_is_valid(transfer, trial.stack, trial.payload)),
            "exit_code": transfer.exit_code,
            "handshake_s": out["time_appconnect"],
            "ttfb_s": out["time_starttransfer"],
            "download_time_s": out["time_total"],
            "throughput_mbps": None if speed is None else speed * 8 / 1e6,
            **out,
            "cpu_user_s": transfer.cpu_user_s,
            "cpu_sys_s": transfer.cpu_sys_s,
            "cpu_pct": transfer.cpu_pct,
            "soc_temp_c": soc_temp_c,
            "throttled_before": throttled_before,
            "throttled_after": host.throttled_flags(),
        }

    def _probe_dish(self) -> dict[str, Any]:
        try:
            with dish.DishClient(self._config.dish_address, timeout_s=dish.CONNECT_TIMEOUT_S) as client:
                return client.status()
        except dish.POLL_ERRORS as error:
            raise SystemExit(
                f"Starlink dish unreachable at {self._config.dish_address} ({dish.describe_error(error)}). "
                "Connect the Pi through the Starlink network, or pass --no-dish for an off-Starlink test."
            ) from error


def _is_valid(transfer: Transfer, stack: Stack, payload: Payload) -> bool:
    out = transfer.write_out
    return (
        transfer.exit_code == 0
        and out["response_code"] == 200
        and str(out["http_version"]) == stack.http_version
        and out["size_download"] == payload.size_bytes
    )


def _announce(number: int, count: int, window: Window) -> str:
    wait = max(0.0, window.start - time.time())
    opens = time.strftime("%H:%M:%S", time.localtime(window.start))
    return f"window {number}/{count}: {window.algorithm} opens {opens} (in {wait:.0f} s)"


def _progress(index: int, total: int, row: dict[str, Any]) -> str:
    prefix = f"[{index:>4}/{total}] {row['run_id']}  {row['cc']:<5} {row['stack']:<8} {row['payload']:<9}"
    if row.get("exit_code") is None:
        return f"{prefix} SKIPPED ({SKIPPED})"
    mbps: Optional[float] = row["throughput_mbps"]
    seconds: Optional[float] = row["download_time_s"]
    handshake: Optional[float] = row["handshake_s"]
    status = "ok" if row["valid"] else f"FAIL(exit {row['exit_code']}: {row['errormsg'] or '-'})"
    throttled = "  THROTTLED" if host.throttled_now(row["throttled_after"]) else ""
    return (
        f"{prefix} {mbps or 0:8.2f} Mbps  {seconds or 0:7.3f} s  hs {handshake or 0:6.3f} s  "
        f"cpu {row['cpu_pct']:5.1f}%  {status}{throttled}"
    )
