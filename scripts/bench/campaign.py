"""Measurement campaign: sender congestion controller x payload size over Starlink.

Design
  Factors   congestion controller of the picoquic sender (one UDP port each) x
            payload size: 2 MiB, dominated by handshake and slow start / startup,
            and 50 MiB, dominated by the steady state and loss recovery.
  Blocking  each round visits every payload size once, in random order, and each
            payload block runs every controller back to back, in random order;
            the seed is recorded. Controllers are compared within a payload size,
            so the block that matters is (round, payload): its transfers fall in
            one short interval and meet nearly the same Starlink conditions, which
            drift with the 15 s satellite reallocation cycle, beam load, weather
            and obstruction. Randomising the order inside each block spreads the
            residual drift evenly over the controllers instead of confounding it
            with one of them, and every block yields one matched sample per
            controller for paired comparisons.
  Spacing   an idle gap precedes every transfer so that queues built by one
            controller drain before the next starts, and so that dish and CPU
            telemetry also capture the idle baseline.
  Validity  a sample is valid only if curl exits 0, the status is 200, the
            protocol is HTTP/3 and the byte count equals the manifest size.
            Invalid samples are kept and flagged: completion rate is a result.

Outputs (results/<UTC start>/)
  meta.json              configuration, server manifest, host provenance, seed
  runs.csv               one row per transfer: curl write-out, CPU time, throttling
  cpu.csv                periodic client CPU and SoC temperature, tagged by run
  dish.csv, dish.jsonl   1 Hz Starlink dish status (projection and raw message)
"""

from __future__ import annotations

import contextlib
import itertools
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from . import cpu, dish, host
from .config import Config
from .curl import WRITE_OUT_FIELDS, Curl, Transfer
from .manifest import MANIFEST_PATH, Instance, Manifest, Payload, url
from .sinks import CsvSink, JsonlSink

RUN_FIELDS = (
    "run_id",
    "round",
    "slot",
    "cc",
    "algorithm",
    "port",
    "payload",
    "payload_bytes",
    "started_at",
    "finished_at",
    "valid",
    "exit_code",
    "throughput_mbps",
    "download_time_s",
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


class Campaign:
    def __init__(self, config: Config, with_dish: bool) -> None:
        self._config = config
        self._with_dish = with_dish
        self._curl = Curl(config.curl_bin, config.tls13_ciphers, config.connect_timeout_s, config.max_time_s)

    def prepare(self) -> Plan:
        manifest_url = url(self._config.server_host, self._config.manifest_port, MANIFEST_PATH)
        manifest = Manifest.parse(self._curl.fetch(manifest_url))
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
            "connection_trace": self._curl.trace(manifest_url),
            "host": host.describe(self._config.server_host, self._config.curl_bin),
            "dish_at_start": dish_status,
        }
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        return Plan(out_dir, manifest, seed)

    def run(self, plan: Plan) -> None:
        out_dir = plan.out_dir
        total = self._config.rounds * len(plan.manifest.instances) * len(plan.manifest.payloads)
        with contextlib.ExitStack() as stack:
            runs = stack.enter_context(CsvSink(out_dir / "runs.csv", RUN_FIELDS))
            cpu_sink = stack.enter_context(CsvSink(out_dir / "cpu.csv", cpu.FIELDS))
            sampler = stack.enter_context(cpu.CpuSampler(cpu_sink, self._config.cpu_interval_s))
            if self._with_dish:
                table = stack.enter_context(CsvSink(out_dir / "dish.csv", dish.FIELDS))
                raw = stack.enter_context(JsonlSink(out_dir / "dish.jsonl"))
                stack.enter_context(dish.DishPoller(self._config.dish_address, self._config.dish_interval_s, table, raw))
            for index, (round_no, slot, instance, payload) in enumerate(self._schedule(plan.manifest, plan.seed), 1):
                time.sleep(self._config.gap_s)
                row = self._measure(sampler, round_no, slot, instance, payload)
                runs.write(row)
                print(_progress(index, total, row), flush=True)

    def _schedule(self, manifest: Manifest, seed: int) -> Iterator[tuple[int, int, Instance, Payload]]:
        rng = random.Random(seed)
        for round_no in range(1, self._config.rounds + 1):
            slots = itertools.count(1)
            for payload in rng.sample(manifest.payloads, len(manifest.payloads)):
                for instance in rng.sample(manifest.instances, len(manifest.instances)):
                    yield round_no, next(slots), instance, payload

    def _measure(self, sampler: cpu.CpuSampler, round_no: int, slot: int, instance: Instance, payload: Payload) -> dict[str, Any]:
        run_id = f"{round_no:03d}-{slot}"
        throttled_before = host.throttled_flags()
        soc_temp_c = host.soc_temperature_c()
        transfer = self._curl.download(
            url(self._config.server_host, instance.port, payload.name),
            on_spawn=lambda pid: sampler.track(run_id, pid),
        )
        sampler.untrack()
        speed = transfer.write_out["speed_download"]
        return {
            "run_id": run_id,
            "round": round_no,
            "slot": slot,
            "cc": instance.label,
            "algorithm": instance.algorithm,
            "port": instance.port,
            "payload": payload.name,
            "payload_bytes": payload.size_bytes,
            "started_at": transfer.started_at,
            "finished_at": transfer.finished_at,
            "valid": int(_is_valid(transfer, payload)),
            "exit_code": transfer.exit_code,
            "throughput_mbps": None if speed is None else speed * 8 / 1e6,
            "download_time_s": transfer.write_out["time_total"],
            **transfer.write_out,
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


def _is_valid(transfer: Transfer, payload: Payload) -> bool:
    out = transfer.write_out
    return (
        transfer.exit_code == 0
        and out["response_code"] == 200
        and str(out["http_version"]) == "3"
        and out["size_download"] == payload.size_bytes
    )


def _progress(index: int, total: int, row: dict[str, Any]) -> str:
    mbps: Optional[float] = row["throughput_mbps"]
    seconds: Optional[float] = row["download_time_s"]
    status = "ok" if row["valid"] else f"FAIL(exit {row['exit_code']}: {row['errormsg'] or '-'})"
    throttled = "  THROTTLED" if host.throttled_now(row["throttled_after"]) else ""
    return (
        f"[{index:>4}/{total}] {row['run_id']}  {row['cc']:<6} {row['payload']:<9} "
        f"{mbps or 0:8.2f} Mbps  {seconds or 0:7.3f} s  cpu {row['cpu_pct']:5.1f}%  {status}{throttled}"
    )
