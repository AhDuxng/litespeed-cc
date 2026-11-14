# Shared environment of the server-side scripts.
#
# Code (server/, replaced on every deploy) is kept apart from generated state
# (build/, www/, tls/, state/, logs/ beside it), so redeploying code never touches
# the compiled binary, the payloads or the collected logs. Instance specifications
# are parsed in one place so the systemd units, the launcher and the manifest
# cannot disagree on which port carries which congestion controller.

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(dirname "$CODE_DIR")"
BUILD_DIR="$ROOT_DIR/build"
WWW_DIR="$ROOT_DIR/www"
TLS_DIR="$ROOT_DIR/tls"
STATE_DIR="$ROOT_DIR/state"
LOG_DIR="$ROOT_DIR/logs"
PICOQUIC_BIN="$BUILD_DIR/picoquicdemo"

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
    local label="$1" field="$2" spec name algorithm port
    for spec in "${INSTANCES[@]}"; do
        IFS=: read -r name algorithm port <<<"$spec"
        [[ $name == "$label" ]] || continue
        case $field in
            algorithm) printf '%s\n' "$algorithm" ;;
            port) printf '%s\n' "$port" ;;
            *) return 1 ;;
        esac
        return 0
    done
    return 1
}
