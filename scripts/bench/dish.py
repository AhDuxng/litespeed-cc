"""Starlink dish telemetry from the terminal's local gRPC API.

The dish answers SpaceX.API.Device.Device/Handle on 192.168.100.1:9200; a
Request carrying get_status returns the dish's own view of the space link. The
schema is unpublished and evolves with firmware, so message types are resolved
at run time through gRPC server reflection rather than compiled stubs, and each
sample is stored twice:

  dish.jsonl  the complete status message, lossless under schema drift;
  dish.csv    a fixed projection of the fields that bear on transport behaviour:
    pop_ping_latency_ms, pop_ping_drop_rate     RTT and loss between the dish
                                                and the Starlink point of presence
    downlink_/uplink_throughput_bps             traffic carried by the dish
    fraction_obstructed, currently_obstructed   sky-view obstruction
    seconds_to_first_nonempty_slot              wait for the next scheduled slot
    snr_above_noise_floor, snr_persistently_low radio link margin
    tilt_angle_deg, boresight_*_deg             antenna attitude
    alerts                                      currently raised alert flags

Fields at their proto3 default are emitted explicitly, so a zero drop rate is
recorded as 0 rather than as missing. A failed poll yields an error row instead
of no row, which keeps telemetry gaps visible in the data.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

import grpc
from google.protobuf import descriptor_pool, json_format, message_factory
from grpc_reflection.v1alpha.proto_reflection_descriptor_database import ProtoReflectionDescriptorDatabase

from .periodic import PeriodicTask
from .sinks import CsvSink, JsonlSink

SERVICE = "SpaceX.API.Device.Device"
METHOD = "Handle"
CONNECT_TIMEOUT_S = 3.0

PROJECTION: Mapping[str, tuple[str, ...]] = {
    "uptime_s": ("device_state", "uptime_s"),
    "pop_ping_latency_ms": ("pop_ping_latency_ms",),
    "pop_ping_drop_rate": ("pop_ping_drop_rate",),
    "downlink_throughput_bps": ("downlink_throughput_bps",),
    "uplink_throughput_bps": ("uplink_throughput_bps",),
    "fraction_obstructed": ("obstruction_stats", "fraction_obstructed"),
    "currently_obstructed": ("obstruction_stats", "currently_obstructed"),
    "seconds_to_first_nonempty_slot": ("seconds_to_first_nonempty_slot",),
    "snr_above_noise_floor": ("is_snr_above_noise_floor",),
    "snr_persistently_low": ("is_snr_persistently_low",),
    "tilt_angle_deg": ("alignment_stats", "tilt_angle_deg"),
    "boresight_azimuth_deg": ("alignment_stats", "boresight_azimuth_deg"),
    "boresight_elevation_deg": ("alignment_stats", "boresight_elevation_deg"),
    "eth_speed_mbps": ("eth_speed_mbps",),
    "software_version": ("device_info", "software_version"),
}
FIELDS = ("ts", "rpc_ms", "error", *PROJECTION, "alerts")
POLL_ERRORS = (grpc.RpcError, grpc.FutureTimeoutError, KeyError)


class DishClient:
    def __init__(self, address: str, timeout_s: float) -> None:
        self._timeout_s = timeout_s
        self._channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(self._channel).result(timeout=CONNECT_TIMEOUT_S)
            pool = descriptor_pool.DescriptorPool(ProtoReflectionDescriptorDatabase(self._channel))
            method = pool.FindServiceByName(SERVICE).FindMethodByName(METHOD)
        except BaseException:
            self._channel.close()
            raise
        self._request_type = message_factory.GetMessageClass(method.input_type)
        response_type = message_factory.GetMessageClass(method.output_type)
        self._handle = self._channel.unary_unary(
            f"/{SERVICE}/{METHOD}",
            request_serializer=self._request_type.SerializeToString,
            response_deserializer=response_type.FromString,
        )

    def __enter__(self) -> DishClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        request = self._request_type()
        request.get_status.SetInParent()
        response = self._handle(request, timeout=self._timeout_s)
        document = json_format.MessageToDict(
            response,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
        return document["dish_get_status"]

    def close(self) -> None:
        self._channel.close()


def _lookup(document: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = document
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def project(status: Mapping[str, Any]) -> dict[str, Any]:
    row = {name: _lookup(status, path) for name, path in PROJECTION.items()}
    alerts = status.get("alerts") or {}
    row["alerts"] = ";".join(sorted(name for name, raised in alerts.items() if raised is True))
    return row


def describe_error(error: BaseException) -> str:
    if isinstance(error, grpc.Call):
        return f"{error.code().name}: {error.details()}"
    if isinstance(error, grpc.FutureTimeoutError):
        return f"no connection within {CONNECT_TIMEOUT_S:g} s"
    return f"{type(error).__name__}: {error}"


class DishPoller(PeriodicTask):
    def __init__(self, address: str, interval_s: float, table: CsvSink, raw: JsonlSink) -> None:
        super().__init__("dish-poller", interval_s)
        self._address = address
        self._table = table
        self._raw = raw
        self._client: Optional[DishClient] = None

    def tick(self, now: float) -> None:
        started = time.monotonic()
        try:
            if self._client is None:
                self._client = DishClient(self._address, timeout_s=self._interval_s)
            status = self._client.status()
        except POLL_ERRORS as error:
            self.close()
            self._table.write({"ts": now, "rpc_ms": _elapsed_ms(started), "error": describe_error(error)})
            return
        self._raw.write({"ts": now, "status": status})
        self._table.write({"ts": now, "rpc_ms": _elapsed_ms(started), "error": "", **project(status)})

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 3)
