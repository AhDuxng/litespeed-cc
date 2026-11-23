# Shared environment of the server-side scripts.
#
# Code (server/, replaced on every deploy) is kept apart from generated state
# (www/, tls/, state/, logs/ beside it), so redeploying code never touches the
# payloads, the certificate, the rendered OpenLiteSpeed configurations or the
# collected logs. Instance, TCP listener and schedule specifications are parsed in
# one place so that the OpenLiteSpeed configurations, the systemd units and the
# manifest cannot disagree on which port carries which congestion control algorithm.

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(dirname "$CODE_DIR")"
WWW_DIR="$ROOT_DIR/www"
TLS_DIR="$ROOT_DIR/tls"
STATE_DIR="$ROOT_DIR/state"
LOG_DIR="$ROOT_DIR/logs"
OLS_STATE_DIR="$STATE_DIR/ols"
OLS_LOG_DIR="$LOG_DIR/ols"

source "$CODE_DIR/server.conf"

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '==> %s\n' "$*"
}

instance_labels() {
    local spec
    for spec in "${INSTANCES[@]}"; do
        printf '%s\n' "${spec%%:*}"
    done
}

instance_field() {
    local label="$1" field="$2" spec name quic_cc udp_port
    for spec in "${INSTANCES[@]}"; do
        IFS=: read -r name quic_cc udp_port <<<"$spec"
        [[ $name == "$label" ]] || continue
        case $field in
            quic_cc) printf '%s\n' "$quic_cc" ;;
            udp_port) printf '%s\n' "$udp_port" ;;
            *) return 1 ;;
        esac
        return 0
    done
    return 1
}

tcp_instance() {
    printf '%s\n' "${TCP_LISTENER%%:*}"
}

tcp_port() {
    printf '%s\n' "${TCP_LISTENER##*:}"
}
