"""Platform state and provenance of the client host.

A Raspberry Pi lowers its ARM clock when the supply voltage sags or the SoC
reaches its thermal limit. The firmware reports this through
`vcgencmd get_throttled`: bits 0-3 flag under-voltage, ARM frequency capping,
throttling and the soft temperature limit as active now, bits 16-19 the same
conditions as having occurred since boot. A throttled receiver processes
packets and decrypts records more slowly and can masquerade as a weaker
congestion controller or a costlier stack, so the flags and the SoC temperature
are recorded around every transfer. Host facts (kernel, curl build, Python
packages, TCP receive-side sysctls, egress route, clock synchronisation) are
stored with each dataset so that results can be traced to the exact software and
network path that produced them.
"""

from __future__ import annotations

import platform
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any, Optional, Sequence

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
DEVICE_MODEL = Path("/proc/device-tree/model")
OS_RELEASE = Path("/etc/os-release")
THROTTLED_NOW_MASK = 0xF
PYTHON_PACKAGES = ("grpcio", "grpcio-reflection", "protobuf")
SYSCTLS = ("net.ipv4.tcp_rmem", "net.core.rmem_max", "net.ipv4.tcp_ecn", "net.ipv4.tcp_congestion_control")


def command_output(argv: Sequence[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read_text(path: Path) -> str:
    try:
        return path.read_text().replace("\0", "").strip()
    except OSError:
        return ""


def soc_temperature_c() -> Optional[float]:
    raw = read_text(THERMAL_ZONE)
    return int(raw) / 1000.0 if raw.isdigit() else None


def throttled_flags() -> str:
    return command_output(["vcgencmd", "get_throttled"]).partition("=")[2]


def throttled_now(flags: str) -> bool:
    return bool(flags) and int(flags, 16) & THROTTLED_NOW_MASK != 0


def _os_name() -> str:
    for line in read_text(OS_RELEASE).splitlines():
        key, _, value = line.partition("=")
        if key == "PRETTY_NAME":
            return value.strip('"')
    return platform.platform()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def describe(server_host: str, curl_bin: Path) -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "model": read_text(DEVICE_MODEL) or platform.machine(),
        "os": _os_name(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "python_packages": {name: _package_version(name) for name in PYTHON_PACKAGES},
        "curl_version": command_output([str(curl_bin), "--version"]).splitlines(),
        "sysctl": {name: read_text(Path("/proc/sys", *name.split("."))) for name in SYSCTLS},
        "egress_route": command_output(["ip", "route", "get", server_host]),
        "ntp_synchronized": command_output(["timedatectl", "show", "--property=NTPSynchronized", "--value"]),
        "throttled": throttled_flags(),
        "soc_temp_c": soc_temperature_c(),
    }
