#!/usr/bin/env bash
# Client provisioning on the Raspberry Pi: a virtual environment with the gRPC
# reflection client used for the Starlink dish, and a check that the configured
# curl negotiates HTTP/3 through ngtcp2, the property every measurement relies on.
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source client.env

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

features="$("$CURL_BIN" --version)"
grep -q 'HTTP3' <<<"$features" || { echo "error: $CURL_BIN lacks HTTP/3" >&2; exit 1; }
grep -q 'ngtcp2' <<<"$features" || { echo "error: $CURL_BIN is not built on ngtcp2" >&2; exit 1; }

printf '%s\n' "$features" | head -1
.venv/bin/python -c 'import grpc, google.protobuf; print("grpcio", grpc.__version__, "protobuf", google.protobuf.__version__)'
