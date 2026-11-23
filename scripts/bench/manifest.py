"""Server manifest: the experiment's ground truth, published by the server.

The server derives www/manifest.json from its own configuration: its certificate
name, the tcp port (HTTP/2) and udp port (HTTP/3) of each congestion control
instance (QUIC algorithm fixed per instance), the time grid on which the kernel TCP algorithm alternates together with
the client window delay, the payload sizes with SHA-256, and the provenance of
the sender. The client takes name, ports, schedule and expected sizes from it
instead of keeping a copy, so a transfer can never be attributed to the wrong
algorithm or validated against a wrong size, and the server-side provenance is
archived with every dataset.

An Endpoint addresses the server by its certificate name pinned to the IP
literal with curl --resolve. No DNS lookup enters the timing, and every stack
sends the same SNI, which lsquic in OpenLiteSpeed requires: it looks the QUIC
certificate up by SNI and aborts a handshake without one with internal_error
(TLS alert 80). The manifest itself is fetched by IP literal over TCP, whose
listener falls back to its default certificate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

MANIFEST_PATH = "manifest.json"


@dataclass(frozen=True)
class Instance:
    label: str
    quic_cc: int
    tcp_port: int
    udp_port: int

    def port(self, transport: str) -> int:
        return self.udp_port if transport == "quic" else self.tcp_port


@dataclass(frozen=True)
class Payload:
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class Switch:
    offset_s: int
    algorithm: str


@dataclass(frozen=True)
class TcpSchedule:
    period_s: int
    switches: tuple[Switch, ...]
    window_delay_s: int


@dataclass(frozen=True)
class Endpoint:
    name: str
    address: str
    port: int

    def url(self, path: str) -> str:
        return f"https://{self.name}:{self.port}/{path.lstrip('/')}"

    def pin(self) -> tuple[str, ...]:
        return () if self.name == self.address else ("--resolve", f"{self.name}:{self.port}:{self.address}")


@dataclass(frozen=True)
class Manifest:
    document: Mapping[str, Any]
    tls_name: str
    instances: tuple[Instance, ...]
    schedule: TcpSchedule
    payloads: tuple[Payload, ...]

    @classmethod
    def parse(cls, raw: bytes) -> Manifest:
        document = json.loads(raw)
        grid = document["tcp_cc_schedule"]
        manifest = cls(
            document=document,
            tls_name=document["server"]["tls_name"],
            instances=tuple(
                Instance(i["label"], int(i["quic_cc"]), int(i["tcp_port"]), int(i["udp_port"])) for i in document["instances"]
            ),
            schedule=TcpSchedule(
                period_s=int(grid["period_s"]),
                switches=tuple(
                    sorted((Switch(int(s["offset_s"]), s["algorithm"]) for s in grid["switches"]), key=lambda s: s.offset_s)
                ),
                window_delay_s=int(grid["window_delay_s"]),
            ),
            payloads=tuple(Payload(p["name"], int(p["bytes"]), p["sha256"]) for p in document["payloads"]),
        )
        unmatched = {s.algorithm for s in manifest.schedule.switches} - {i.label for i in manifest.instances}
        if unmatched:
            raise ValueError(f"manifest schedules algorithms without an instance: {sorted(unmatched)}")
        return manifest

    def instance(self, label: str) -> Instance:
        return next(instance for instance in self.instances if instance.label == label)

    def endpoint(self, address: str, instance: Instance, transport: str) -> Endpoint:
        return Endpoint(self.tls_name, address, instance.port(transport))
