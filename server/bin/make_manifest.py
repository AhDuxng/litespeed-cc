#!/usr/bin/env python3
"""Generation of www/manifest.json, the experiment's ground truth read by the client.

The manifest binds every identifier the client relies on to the server's actual
configuration: the certificate name every client must send as SNI, the udp port
(HTTP/3) of each congestion control instance, the single tcp listener (HTTP/2)
and the instance that hosts it, the time grid of the kernel TCP algorithm with
the client window delay, and the size and SHA-256 digest of every payload. It also carries the provenance of the sender:
the OpenLiteSpeed image digest and version banner (which names the lsquic
release), the kernel release and the sysctls that shape TCP and UDP sending
(qdisc, socket buffer limits, ECN, per-destination metrics caching). The client
archives the document with every dataset, so a result can always be traced to
the server configuration that produced it. The file is replaced atomically, so a
client never reads a partial document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

SYSCTLS = (
    "net.core.default_qdisc",
    "net.core.rmem_max",
    "net.core.wmem_max",
    "net.ipv4.tcp_available_congestion_control",
    "net.ipv4.tcp_rmem",
    "net.ipv4.tcp_wmem",
    "net.ipv4.tcp_ecn",
    "net.ipv4.tcp_no_metrics_save",
)


def sysctl(name: str) -> str:
    return Path("/proc/sys", *name.split(".")).read_text().strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def instance(spec: str) -> dict[str, Any]:
    label, quic_cc, udp_port = spec.split(":")
    return {"label": label, "quic_cc": int(quic_cc), "udp_port": int(udp_port)}


def tcp_listener(spec: str) -> dict[str, Any]:
    label, port = spec.split(":")
    return {"instance": label, "port": int(port)}


def switch(spec: str) -> dict[str, Any]:
    offset, algorithm = spec.split(":")
    return {"offset_s": int(offset), "algorithm": algorithm}


def payload(www: Path, name: str) -> dict[str, Any]:
    path = www / name
    return {"name": name, "bytes": path.stat().st_size, "sha256": sha256(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the experiment manifest.")
    parser.add_argument("--www", type=Path, required=True)
    parser.add_argument("--tls-name", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--server-version", default="")
    parser.add_argument("--instance", action="append", required=True, metavar="LABEL:QUIC_CC:UDP_PORT")
    parser.add_argument("--tcp", required=True, metavar="LABEL:PORT")
    parser.add_argument("--period", type=int, required=True)
    parser.add_argument("--switch", action="append", required=True, metavar="OFFSET_S:ALGORITHM")
    parser.add_argument("--window-delay", type=int, required=True)
    parser.add_argument("--payload", action="append", required=True, metavar="FILE")
    args = parser.parse_args()

    document = {
        "generated_at": time.time(),
        "server": {
            "tls_name": args.tls_name,
            "image": args.image,
            "image_digest": args.image_digest,
            "version": args.server_version.splitlines(),
            "kernel": platform.release(),
            "sysctl": {name: sysctl(name) for name in SYSCTLS},
        },
        "instances": [instance(spec) for spec in args.instance],
        "tcp": tcp_listener(args.tcp),
        "tcp_cc_schedule": {
            "period_s": args.period,
            "switches": [switch(spec) for spec in args.switch],
            "window_delay_s": args.window_delay,
        },
        "payloads": [payload(args.www, name) for name in args.payload],
    }
    target = args.www / "manifest.json"
    staging = target.with_suffix(".json.tmp")
    staging.write_text(json.dumps(document, indent=2) + "\n")
    os.replace(staging, target)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
