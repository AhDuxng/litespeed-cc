#!/usr/bin/env bash
# Idempotent provisioning of the picoquic congestion-control server.
#
# Usage: setup.sh [step ...]          (no argument: every step, in order)
#   packages  toolchain and OpenSSL headers required by picoquic/picotls
#   picoquic  build picoquicdemo at the pinned commit; skipped when already built
#   tls       self-signed P-256 certificate (the client runs curl --insecure)
#   payloads  incompressible random files; regenerated only when the size differs
#   manifest  www/manifest.json, the single source of truth read by the client:
#             controller-to-port map, payload sizes and SHA-256, picoquic commit
#   services  render and (re)start picoquic@<label> and picoquic-cpu
#   verify    every instance is active and bound to its UDP port
set -euo pipefail

source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/common.sh"

readonly ALL_STEPS=(packages picoquic tls payloads manifest services verify)
readonly PACKAGES=(build-essential cmake git pkg-config libssl-dev python3)
readonly SYSTEMD_DIR=/etc/systemd/system

step_packages() {
    local pkg missing=()
    for pkg in "${PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
    done
    if ((${#missing[@]} == 0)); then
        log "packages already installed"
        return
    fi
    log "installing ${missing[*]}"
    sudo apt-get update -q
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
}

step_picoquic() {
    local src="$BUILD_DIR/picoquic" stamp="$BUILD_DIR/picoquicdemo.commit"
    if [[ -x $PICOQUIC_BIN && -f $stamp && $(<"$stamp") == "$PICOQUIC_COMMIT" ]]; then
        log "picoquicdemo already built at ${PICOQUIC_COMMIT:0:12}"
        return
    fi
    mkdir -p "$BUILD_DIR"
    [[ -d $src/.git ]] || git clone --quiet "$PICOQUIC_REPO" "$src"
    git -C "$src" fetch --quiet origin
    git -C "$src" checkout --quiet --force "$PICOQUIC_COMMIT"
    cmake -S "$src" -B "$src/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DPICOQUIC_FETCH_PTLS=ON \
        -Dpicoquic_BUILD_TESTS=OFF
    cmake --build "$src/build" --parallel "$(nproc)" --target picoquicdemo
    install -m 0755 "$src/build/picoquicdemo" "$PICOQUIC_BIN"
    printf '%s\n' "$PICOQUIC_COMMIT" >"$stamp"
}

step_tls() {
    if [[ -f $TLS_DIR/cert.pem && -f $TLS_DIR/key.pem ]]; then
        log "certificate already present"
        return
    fi
    mkdir -p "$TLS_DIR"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
        -subj "/CN=$TLS_NAME" \
        -addext "subjectAltName=DNS:$TLS_NAME,IP:$PUBLIC_IP" \
        -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" 2>/dev/null
    chmod 600 "$TLS_DIR/key.pem"
}

step_payloads() {
    local spec name size bytes target
    mkdir -p "$WWW_DIR"
    for spec in "${PAYLOADS[@]}"; do
        IFS=: read -r name size <<<"$spec"
        target="$WWW_DIR/$name"
        bytes="$(numfmt --from=iec "$size")"
        if [[ -f $target && $(stat -c %s "$target") == "$bytes" ]]; then
            log "$name already present ($bytes bytes)"
            continue
        fi
        log "generating $name ($bytes bytes)"
        head -c "$bytes" /dev/urandom >"$target.tmp"
        mv "$target.tmp" "$target"
    done
}

json_join() {
    local IFS=,
    printf '%s' "$*"
}

step_manifest() {
    local spec name algorithm port file instances=() payloads=()
    for spec in "${INSTANCES[@]}"; do
        IFS=: read -r name algorithm port <<<"$spec"
        instances+=("$(printf '{"label":"%s","algorithm":"%s","port":%d}' "$name" "$algorithm" "$port")")
    done
    for spec in "${PAYLOADS[@]}"; do
        file="$WWW_DIR/${spec%%:*}"
        [[ -f $file ]] || die "missing payload $file (run the payloads step)"
        payloads+=("$(printf '{"name":"%s","bytes":%d,"sha256":"%s"}' \
            "${spec%%:*}" "$(stat -c %s "$file")" "$(sha256sum "$file" | cut -d' ' -f1)")")
    done
    printf '{"picoquic_commit":"%s","socket_buffer_bytes":%d,"qlog":%s,"instances":[%s],"payloads":[%s]}\n' \
        "$PICOQUIC_COMMIT" "$SOCKET_BUFFER_BYTES" "$([[ $QLOG == 1 ]] && echo true || echo false)" \
        "$(json_join "${instances[@]}")" "$(json_join "${payloads[@]}")" >"$WWW_DIR/manifest.json"
    log "wrote $WWW_DIR/manifest.json"
}

render_unit() {
    local template="$1" monitor_units="$2"
    sed -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        -e "s|@CODE_DIR@|$CODE_DIR|g" \
        -e "s|@STATE_DIR@|$STATE_DIR|g" \
        -e "s|@LOG_DIR@|$LOG_DIR|g" \
        -e "s|@MONITOR_UNITS@|$monitor_units|g" \
        "$template" | sudo tee "$SYSTEMD_DIR/$(basename "$template")" >/dev/null
}

service_units() {
    local label
    while read -r label; do
        printf 'picoquic@%s.service\n' "$label"
    done < <(instance_labels)
    printf 'picoquic-cpu.service\n'
}

step_services() {
    local label units monitor_units=()
    [[ -x $PICOQUIC_BIN ]] || die "picoquicdemo not built (run the picoquic step)"
    mkdir -p "$STATE_DIR" "$LOG_DIR"
    while read -r label; do
        monitor_units+=("$label=picoquic@$label.service")
    done < <(instance_labels)
    render_unit "$CODE_DIR/systemd/picoquic@.service" ""
    render_unit "$CODE_DIR/systemd/picoquic-cpu.service" "${monitor_units[*]}"
    mapfile -t units < <(service_units)
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet "${units[@]}"
    sudo systemctl restart "${units[@]}"
    log "restarted ${units[*]}"
}

step_verify() {
    local unit label port failed=0
    sleep 2
    while read -r unit; do
        if systemctl is-active --quiet "$unit"; then
            log "$unit active"
        else
            printf 'FAIL: %s is not active\n' "$unit" >&2
            failed=1
        fi
    done < <(service_units)
    while read -r label; do
        port="$(instance_field "$label" port)"
        if ss -Hlun "sport = :$port" | grep -q .; then
            log "$label listening on udp/$port"
        else
            printf 'FAIL: nothing bound to udp/%s (%s)\n' "$port" "$label" >&2
            failed=1
        fi
    done < <(instance_labels)
    ((failed == 0)) || die "verification failed; inspect: journalctl -u 'picoquic*'"
}

main() {
    local step steps=("$@")
    ((${#steps[@]})) || steps=("${ALL_STEPS[@]}")
    for step in "${steps[@]}"; do
        declare -F "step_$step" >/dev/null || die "unknown step '$step' (valid: ${ALL_STEPS[*]})"
        log "step: $step"
        "step_$step"
    done
}

main "$@"
