"""Starlink end-to-end download campaign against OpenLiteSpeed.

A Raspberry Pi behind a Starlink terminal downloads fixed payloads over HTTP/2
on TCP with TLS 1.2 or TLS 1.3 and over HTTP/3 on QUIC, while the server's
congestion control alternates between CUBIC and BBRv1, recording curl transfer
timings, client CPU load and the dish's own link telemetry at 1 Hz.
"""
