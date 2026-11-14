"""Starlink HTTP/3 download campaign comparing sender congestion controllers.

A Raspberry Pi behind a Starlink terminal downloads fixed payloads from picoquic
servers that differ only in their congestion controller, while recording curl
transfer timings, client CPU load and the dish's own link telemetry at 1 Hz.
"""
