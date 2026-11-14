#!/usr/bin/env bash
# One picoquic HTTP/3 static-file server whose sender runs a single congestion
# controller (-G). Instances differ only in -G and the UDP port; certificate, web
# root, socket buffers, MTU discovery and pacing are shared, so a throughput
# difference between ports is attributable to the controller alone.
#
# picoquicdemo keeps its ticket and token stores in the working directory, so
# every instance runs from its own state directory to keep the stores disjoint.
# With QLOG=1 each connection leaves a sender-side qlog (cwnd, RTT samples, loss
# events) under logs/qlog/<label>/.
set -euo pipefail

source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib/common.sh"

label="${1:?usage: serve.sh <instance-label>}"
algorithm="$(instance_field "$label" algorithm)" || die "unknown instance '$label'"
port="$(instance_field "$label" port)"

args=(
    -G "$algorithm"
    -p "$port"
    -w "$WWW_DIR"
    -c "$TLS_DIR/cert.pem"
    -k "$TLS_DIR/key.pem"
    -B "$SOCKET_BUFFER_BYTES"
)
if [[ $QLOG == 1 ]]; then
    mkdir -p "$LOG_DIR/qlog/$label"
    args+=(-q "$LOG_DIR/qlog/$label")
fi

mkdir -p "$STATE_DIR/$label"
cd "$STATE_DIR/$label"
exec "$PICOQUIC_BIN" "${args[@]}"
