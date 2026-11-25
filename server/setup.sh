#!/usr/bin/env bash
# Idempotent provisioning of the OpenLiteSpeed congestion-control server.
#
# Usage: setup.sh [step ...]          (no argument: every step, in order)
#   prereq    docker daemon reachable, SERVICE_USER in the docker group, python3
#   kernel    every algorithm label loaded as a kernel TCP congestion control and
#             loaded again at boot; tcp_no_metrics_save=1, so a TCP connection does
#             not inherit ssthresh and RTT cached from the previous connection to the
#             same address, which every transfer of the campaign shares
#   image     pull the pinned OpenLiteSpeed image when absent
#   tls       self-signed P-256 certificate (the client runs curl --insecure)
#   payloads  incompressible random files; regenerated only when the size differs
#   config    render one OpenLiteSpeed configuration per instance; the instance named
#             in TCP_LISTENER also gets the single HTTP/2 over TLS listener
#   manifest  www/manifest.json, the single source of truth read by the client
#   services  render and (re)start ols@<label>, ols-cpu and tcp-cc.timer
#   verify    containers active, the tcp port and every udp port bound, HTTP/2 over
#             TLS 1.2 and 1.3 answered locally, kernel algorithm equal to the scheduled one
set -euo pipefail

source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/common.sh"

readonly ALL_STEPS=(prereq kernel image tls payloads config manifest services verify)
readonly SYSTEMD_DIR=/etc/systemd/system
readonly UNIT_TEMPLATES=(ols@.service ols-cpu.service tcp-cc.service tcp-cc.timer)
readonly MODULES_FILE=/etc/modules-load.d/starlink-cc.conf
readonly SYSCTL_FILE=/etc/sysctl.d/90-starlink-cc.conf

render() {
    local template="$1" pair expressions=()
    shift
    for pair in "$@"; do
        expressions+=(-e "s|@${pair%%=*}@|${pair#*=}|g")
    done
    sed "${expressions[@]}" "$template"
}

on_calendar() {
    local spec offset algorithm lines=() joined
    ((TCP_CC_PERIOD_S % 60 == 0 && 3600 % TCP_CC_PERIOD_S == 0)) ||
        die "TCP_CC_PERIOD_S must be a whole number of minutes that divides one hour"
    for spec in "${TCP_CC_SWITCHES[@]}"; do
        IFS=: read -r offset algorithm <<<"$spec"
        ((offset < TCP_CC_PERIOD_S)) || die "switch '$spec' must lie inside the period"
        instance_field "$algorithm" quic_cc >/dev/null || die "switch '$spec' names no instance"
        lines+=("$(printf 'OnCalendar=*-*-* *:%02d/%d:%02d' $((offset / 60)) $((TCP_CC_PERIOD_S / 60)) $((offset % 60)))")
    done
    joined="$(printf '%s\\n' "${lines[@]}")"
    printf '%s' "${joined%\\n}"
}

tcp_cc_args() {
    local spec args=(--period "$TCP_CC_PERIOD_S")
    for spec in "${TCP_CC_SWITCHES[@]}"; do
        args+=(--switch "$spec")
    done
    printf '%s' "${args[*]}"
}

step_prereq() {
    command -v python3 >/dev/null || die "python3 is required"
    command -v docker >/dev/null || die "docker is required"
    id -nG "$SERVICE_USER" | grep -qw docker || die "$SERVICE_USER must belong to the docker group"
    docker info >/dev/null 2>&1 || die "docker daemon unreachable for $(id -un)"
    log "docker $(docker version --format '{{.Server.Version}}'), $(python3 --version)"
}

step_kernel() {
    local label module modules=()
    for label in $(instance_labels); do
        module="tcp_$label"
        grep -qw "$label" /proc/sys/net/ipv4/tcp_available_congestion_control || sudo modprobe "$module"
        grep -qw "$label" /proc/sys/net/ipv4/tcp_available_congestion_control || die "kernel lacks $module"
        if [[ $(modinfo -F filename "$module" 2>/dev/null) == /* ]]; then
            modules+=("$module")
        fi
    done
    printf '%s\n' "${modules[@]}" | sudo tee "$MODULES_FILE" >/dev/null
    printf 'net.ipv4.tcp_no_metrics_save = 1\n' | sudo tee "$SYSCTL_FILE" >/dev/null
    sudo sysctl --quiet --load "$SYSCTL_FILE"
    log "tcp algorithms: $(</proc/sys/net/ipv4/tcp_available_congestion_control); boot modules: ${modules[*]:-none}"
}

step_image() {
    if docker image inspect "$OLS_IMAGE" >/dev/null 2>&1; then
        log "$OLS_IMAGE already present"
        return
    fi
    docker pull --quiet "$OLS_IMAGE"
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

step_config() {
    local label dir
    instance_field "$(tcp_instance)" quic_cc >/dev/null || die "TCP_LISTENER '$TCP_LISTENER' names no instance"
    for label in $(instance_labels); do
        dir="$OLS_STATE_DIR/$label"
        mkdir -p "$dir" "$OLS_LOG_DIR/$label"
        {
            render "$CODE_DIR/ols/httpd_config.conf.in" \
                "LABEL=$label" \
                "UDP_PORT=$(instance_field "$label" udp_port)" \
                "QUIC_CC=$(instance_field "$label" quic_cc)"
            if [[ $label == "$(tcp_instance)" ]]; then
                printf '\n'
                render "$CODE_DIR/ols/listener_tls.conf.in" "TCP_PORT=$(tcp_port)"
            fi
        } >"$dir/httpd_config.conf"
        log "rendered $dir/httpd_config.conf"
    done
}

step_manifest() {
    local spec digest version args=()
    for spec in "${INSTANCES[@]}"; do
        args+=(--instance "$spec")
    done
    for spec in "${TCP_CC_SWITCHES[@]}"; do
        args+=(--switch "$spec")
    done
    for spec in "${PAYLOADS[@]}"; do
        [[ -f $WWW_DIR/${spec%%:*} ]] || die "missing payload $WWW_DIR/${spec%%:*} (run the payloads step)"
        args+=(--payload "${spec%%:*}")
    done
    digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$OLS_IMAGE" 2>/dev/null || true)"
    version="$(timeout 30 docker run --rm --network none --entrypoint /usr/local/lsws/bin/openlitespeed "$OLS_IMAGE" -v 2>&1 || true)"
    python3 "$CODE_DIR/bin/make_manifest.py" \
        --www "$WWW_DIR" \
        --tls-name "$TLS_NAME" \
        --image "$OLS_IMAGE" \
        --image-digest "$digest" \
        --server-version "$version" \
        --period "$TCP_CC_PERIOD_S" \
        --window-delay "$WINDOW_DELAY_S" \
        --tcp "$TCP_LISTENER" \
        "${args[@]}"
}

server_units() {
    local label
    for label in $(instance_labels); do
        printf 'ols@%s.service\n' "$label"
    done
    printf 'ols-cpu.service\n'
}

step_services() {
    local label name units containers=() variables
    for label in $(instance_labels); do
        [[ -f $OLS_STATE_DIR/$label/httpd_config.conf ]] || die "no configuration for $label (run the config step)"
        containers+=("$label=ols-$label")
    done
    variables=(
        "SERVICE_USER=$SERVICE_USER"
        "CODE_DIR=$CODE_DIR"
        "LOG_DIR=$LOG_DIR"
        "OLS_STATE_DIR=$OLS_STATE_DIR"
        "OLS_LOG_DIR=$OLS_LOG_DIR"
        "TLS_DIR=$TLS_DIR"
        "WWW_DIR=$WWW_DIR"
        "OLS_IMAGE=$OLS_IMAGE"
        "MONITOR_CONTAINERS=${containers[*]}"
        "TCP_CC_ARGS=$(tcp_cc_args)"
        "ON_CALENDAR=$(on_calendar)"
    )
    mkdir -p "$LOG_DIR"
    for name in "${UNIT_TEMPLATES[@]}"; do
        render "$CODE_DIR/systemd/$name" "${variables[@]}" | sudo tee "$SYSTEMD_DIR/$name" >/dev/null
    done
    mapfile -t units < <(server_units)
    sudo systemctl daemon-reload
    sudo systemctl start tcp-cc.service
    sudo systemctl enable --quiet tcp-cc.timer "${units[@]}"
    sudo systemctl restart tcp-cc.timer "${units[@]}"
    log "restarted tcp-cc.timer ${units[*]}"
}

check() {
    local description="$1"
    shift
    if "$@"; then
        log "$description"
    else
        printf 'FAIL: %s\n' "$description" >&2
        return 1
    fi
}

answers_h2() {
    local port="$1" version="$2"
    [[ $(curl -sk --http2 --tlsv"$version" --tls-max "$version" --max-time 5 -o /dev/null \
        -w '%{http_code} %{http_version}' "https://127.0.0.1:$port/manifest.json") == "200 2" ]]
}

scheduled_cc_in_force() {
    local scheduled
    scheduled="$(tail -n 1 "$LOG_DIR/tcp_cc.csv" | cut -d, -f3)"
    [[ -n $scheduled && $(</proc/sys/net/ipv4/tcp_congestion_control) == "$scheduled" ]]
}

step_verify() {
    local unit label udp_port version failed=0
    sleep 3
    while read -r unit; do
        check "$unit active" systemctl is-active --quiet "$unit" || failed=1
    done < <(server_units; printf 'tcp-cc.timer\n')
    for label in $(instance_labels); do
        udp_port="$(instance_field "$label" udp_port)"
        check "$label bound to udp/$udp_port" bash -c "ss -Hlun 'sport = :$udp_port' | grep -q ." || failed=1
    done
    check "$(tcp_instance) bound to tcp/$(tcp_port)" bash -c "ss -Hltn 'sport = :$(tcp_port)' | grep -q ." || failed=1
    for version in 1.2 1.3; do
        check "tcp/$(tcp_port) answers HTTP/2 over TLS $version" answers_h2 "$(tcp_port)" "$version" || failed=1
    done
    check "kernel TCP algorithm $(</proc/sys/net/ipv4/tcp_congestion_control) matches the schedule" scheduled_cc_in_force || failed=1
    ((failed == 0)) || die "verification failed; inspect: journalctl -u 'ols@*' -u tcp-cc; docker logs ols-<label>"
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
