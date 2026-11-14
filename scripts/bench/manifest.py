"""Server manifest: the experiment's ground truth, published by the server.

The server derives www/manifest.json from its own configuration: the mapping
from UDP port to congestion controller, the payload sizes with SHA-256 and the
picoquic commit. The client takes controller labels, ports and expected sizes
from it instead of keeping a copy, so a transfer can never be attributed to the
wrong controller or validated against a wrong size, and the server-side
provenance is archived with every dataset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

MANIFEST_PATH = "manifest.json"


@dataclass(frozen=True)
class Instance:
    label: str
    algorithm: str
    port: int


@dataclass(frozen=True)
class Payload:
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    document: Mapping[str, Any]
    instances: tuple[Instance, ...]
    payloads: tuple[Payload, ...]

    @classmethod
    def parse(cls, raw: bytes) -> Manifest:
        document = json.loads(raw)
        return cls(
            document=document,
            instances=tuple(Instance(i["label"], i["algorithm"], int(i["port"])) for i in document["instances"]),
            payloads=tuple(Payload(p["name"], int(p["bytes"]), p["sha256"]) for p in document["payloads"]),
        )


def url(host: str, port: int, path: str) -> str:
    return f"https://{host}:{port}/{path.lstrip('/')}"
