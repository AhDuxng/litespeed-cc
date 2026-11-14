"""Campaign configuration.

Every tunable of a campaign comes from one KEY=VALUE file (client.env); a
variable of the same name in the process environment overrides the file, which
permits a short smoke test without editing it. The effective configuration is
stored in each results directory, so a dataset always carries the parameters
that produced it.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


@dataclasses.dataclass(frozen=True)
class Config:
    server_host: str
    manifest_port: int
    curl_bin: Path
    tls13_ciphers: str
    rounds: int
    gap_s: float
    connect_timeout_s: float
    max_time_s: float
    dish_address: str
    dish_interval_s: float
    cpu_interval_s: float
    results_dir: Path
    seed: Optional[int]

    def as_dict(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Path) else value for key, value in dataclasses.asdict(self).items()}


def _optional_int(value: str) -> Optional[int]:
    return int(value) if value else None


def _path(value: str) -> Path:
    return Path(value).expanduser()


_SCHEMA: Mapping[str, tuple[str, Callable[[str], Any]]] = {
    "SERVER_HOST": ("server_host", str),
    "MANIFEST_PORT": ("manifest_port", int),
    "CURL_BIN": ("curl_bin", _path),
    "TLS13_CIPHERS": ("tls13_ciphers", str),
    "ROUNDS": ("rounds", int),
    "GAP_SECONDS": ("gap_s", float),
    "CONNECT_TIMEOUT_SECONDS": ("connect_timeout_s", float),
    "MAX_TIME_SECONDS": ("max_time_s", float),
    "DISH_ADDRESS": ("dish_address", str),
    "DISH_INTERVAL_SECONDS": ("dish_interval_s", float),
    "CPU_INTERVAL_SECONDS": ("cpu_interval_s", float),
    "RESULTS_DIR": ("results_dir", _path),
    "SEED": ("seed", _optional_int),
}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load(path: Path, environ: Mapping[str, str] = os.environ) -> Config:
    raw = read_env_file(path)
    raw.update({key: environ[key] for key in _SCHEMA if key in environ})
    missing = sorted(set(_SCHEMA) - set(raw))
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    config = Config(**{attribute: parse(raw[key]) for key, (attribute, parse) in _SCHEMA.items()})
    if not config.results_dir.is_absolute():
        config = dataclasses.replace(config, results_dir=path.resolve().parent / config.results_dir)
    return config
