#!/usr/bin/env bash
# Client provisioning on the Raspberry Pi: a virtual environment with the gRPC
# reflection client used for the Starlink dish, and a check that the configured
# curl speaks HTTP/2 (nghttp2) and HTTP/3 through ngtcp2, the properties every
# measurement relies on.
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source client.env

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

features="$("$CURL_BIN" --version)"
for feature in HTTP2 HTTP3 ngtcp2; do
    grep -q "$feature" <<<"$features" || { echo "error: $CURL_BIN lacks $feature" >&2; exit 1; }
done

printf '%s\n' "$features" | head -1
.venv/bin/python -c 'import grpc, google.protobuf; print("grpcio", grpc.__version__, "protobuf", google.protobuf.__version__)'
