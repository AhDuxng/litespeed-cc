"""Protocol stacks under test and the curl options that pin each one.

  h2-tls12  HTTP/2 over TCP + TLS 1.2   --http2 --tlsv1.2 --tls-max 1.2 --ciphers <TLS 1.2 suite>
  h2-tls13  HTTP/2 over TCP + TLS 1.3   --http2 --tlsv1.3 --tls-max 1.3 --tls13-ciphers <TLS 1.3 suite>
  h3        HTTP/3 over QUIC (TLS 1.3)  --http3-only --tls13-ciphers <TLS 1.3 suite>
  every stack                           --curves <group>

Floor and ceiling of the TLS version are both pinned, so neither the server nor
a middlebox can move a stack to another version. --http3-only forbids the silent
fallback to TCP that --http3 permits. For HTTP/2 the ALPN outcome is checked
after the transfer (http_version must be 2), because curl would otherwise carry
on over HTTP/1.1. All stacks use the same AEAD, ChaCha20-Poly1305: the Pi 4
Cortex-A72 lacks the ARMv8 cryptography extensions, so AES-GCM would run in
scalar software and receiver CPU would differ between stacks for a reason
unrelated to the transport. All stacks also share one key exchange group (X25519,
then P-256, which TLS 1.2 needs to accept the server's ECDSA certificate): the
default hybrid post-quantum share of OpenSSL 3.5 would lengthen the ClientHello
beyond one QUIC packet, a handshake difference unrelated to the stacks under
test, and lsquic fails most such handshakes. Every transfer is a new curl process without a
session cache, hence a full handshake with neither TLS resumption nor 0-RTT.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stack:
    name: str
    transport: str
    http_version: str
    curl_args: tuple[str, ...]


def stacks(tls12_ciphers: str, tls13_ciphers: str, groups: str) -> tuple[Stack, ...]:
    shared = ("--curves", groups)
    return (
        Stack("h2-tls12", "tcp", "2", ("--http2", "--tlsv1.2", "--tls-max", "1.2", "--ciphers", tls12_ciphers, *shared)),
        Stack("h2-tls13", "tcp", "2", ("--http2", "--tlsv1.3", "--tls-max", "1.3", "--tls13-ciphers", tls13_ciphers, *shared)),
        Stack("h3", "quic", "3", ("--http3-only", "--tls13-ciphers", tls13_ciphers, *shared)),
    )
