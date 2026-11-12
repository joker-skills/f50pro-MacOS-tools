# Internal functions; service.sh holds lifecycle.lock. Sourcing has no side effects.
recovery_now() { awk -F. 'NR == 1 {print $1}' /proc/uptime; }
recovery_boot() { cat /proc/sys/kernel/random/boot_id; }
recovery_remaining() {
    local now
    now=$(recovery_now) && recovery_uint "$now" || return 1
    # Uptime is rounded down; leave one second for the fractional part.
    [ "$((rec_deadline - now))" -gt 1 ] || return 3
    printf '%s\n' "$((rec_deadline - now - 1))"
}

recovery_timeout() {
    local limit=5 kill=2
    if [ -n "${rec_deadline:-}" ]; then
        limit=$(recovery_remaining) || return "$?"
        [ "$limit" -gt 1 ] || return 3
        kill=1
        limit=$((limit - kill))
        [ "$limit" -le 5 ] || limit=5
    fi
    # Status 3 means the deadline prevented starting the command.
    "${BB:-/data/adb/magisk/busybox}" timeout -k "$kill" "$limit" "$@" || return 1
}
recovery_setting() { recovery_timeout /system/bin/settings get global "$1" 2>/dev/null; }
recovery_phone() { recovery_timeout /system/bin/cmd phone data "$1" >/dev/null 2>&1; }
recovery_flush() {
    recovery_timeout "${BB:-/data/adb/magisk/busybox}" sync "$rec_dir/state" "$rec_dir/pending" "$rec_dir" 2>/dev/null
}

recovery_allowed() {
    rec_reason=settings-unknown
    rec_data=$(recovery_setting mobile_data) || {
        [ "$?" != 3 ] || return 3
        rec_data=unknown
    }
    rec_air=$(recovery_setting airplane_mode_on) || {
        # An observed manual-off or unknown value still blocks retry.
        if [ "$?" = 3 ] && [ "$rec_data" = 1 ]; then return 3; fi
        rec_air=unknown
    }
    case "$rec_data" in 0|1) ;; *) rec_data=unknown;; esac
    case "$rec_air" in 0|1) ;; *) rec_air=unknown;; esac
    case "$rec_data:$rec_air" in
        0:0) rec_reason=manual-off; return 1;;
        0:1|1:1) rec_reason=airplane-mode; return 1;;
        1:0) ;;
        *) return 1;;
    esac
    rec_reason=no-cellular-uplink
    if [ -n "${rec_deadline:-}" ]; then
        # The service's read-only uplink action runs before the lifecycle lock.
        rec_if=$(recovery_timeout "${BB:-/data/adb/magisk/busybox}" ash "$ROOT/service.sh" uplink 2>/dev/null) || return 1
    else
        rec_if=$(uplink 2>/dev/null) || return 1
    fi
    rec_reason=no-curl
    command -v curl >/dev/null
}

# HEAD gets the TLS and HTTP result without transferring or retaining a body.
# -q suppresses curlrc; if! forbids fallback to resolving the interface name.
recovery_probe() {
    local limit
    rec_probes=
    rec_invalid=0
    for rec_host in www.baidu.com www.qq.com www.jd.com; do
        limit=8
        if [ -n "${rec_deadline:-}" ]; then
            if ! limit=$(recovery_remaining); then
                [ "$rec_invalid" = 0 ] || return 2
                return 3
            fi
            [ "$limit" -le 8 ] || limit=8
        fi
        rec_rc=0
        rec_metrics=$(curl -q -4 --silent --noproxy '*' --interface "if!$rec_if" \
            --proto '=https' --head --output /dev/null --max-time "$limit" --connect-timeout 5 \
            --write-out '%{remote_ip} %{http_code} %{time_connect} %{time_appconnect} %{time_total}' \
            "https://$rec_host/" 2>/dev/null) || rec_rc=$?
        # Curl write-out contains only numeric transport fields, with an optional IPv4 address.
        case "$rec_metrics" in ''|*[!0-9.\ ]*) rec_metrics='unavailable';; esac
        [ "${#rec_metrics}" -le 100 ] || rec_metrics='unavailable'
        rec_probes="${rec_probes}${rec_probes:+; }$rec_host rc=$rec_rc ip/http/connect/tls/total=$rec_metrics"
        if [ "$rec_rc" = 0 ] && printf '%s\n' "$rec_metrics" | awk 'NF >= 4 && $(NF-3) ~ /^[1-5][0-9][0-9]$/ {ok=1} END {exit !ok}'; then
            return 0
        fi
        # An unusable local probe is not evidence of a cellular outage.
        case "$rec_rc" in 0|1|2|3|4|27|43|45|48|77|126|127) rec_invalid=1;; esac
    done
    [ "$rec_invalid" = 0 ] || return 2
    return 1
}

recovery_log() {
    local size
    size=$(wc -c 2>/dev/null < "$rec_dir/events.log") || size=0
    if [ "$size" -ge 64512 ]; then
        mv -f "$rec_dir/events.log" "$rec_dir/events.previous" || return 1
    fi
    printf '%s boot=%s uptime=%s %s\n' "$(date -u +%FT%TZ)" "$rec_boot" "$rec_now" "$*" >> "$rec_dir/events.log"
}

recovery_capture() {
    # Ten fixed slots, at most 8 KiB each. Never sample packet payloads or raw logs.
    df -Pk "$rec_dir" | awk 'NR == 2 && $4 >= 1024 {ok=1} END {exit !ok}' || return 1
    local slot
    slot=$(cat "$rec_dir/slot" 2>/dev/null) || slot=9
    case "$slot" in [0-9]) ;; *) slot=9;; esac
    slot=$(((slot + 1) % 10))
    rec_incident="$rec_dir/incident.$slot"
    (
        printf '%s boot=%s uptime=%s interface=%s mobile_data=%s airplane_mode=%s\n%s\n' \
            "$(date -u +%FT%TZ)" "$rec_boot" "$rec_now" "$rec_if" "$rec_data" "$rec_air" "$rec_probes"
        /system/bin/ip -4 addr show dev "$rec_if" | awk '$1=="inet" {print "address", $2}'
        /system/bin/ip -4 route show table all dev "$rec_if"
        for name in mihomo tailscaled; do
            if read -r p start 2>/dev/null < "$ROOT/$name.pid"; then
                case "$p:$start" in *[!0-9:]*) ;; *) printf '%s pid/start=%s/%s\n' "$name" "$p" "$start";; esac
            fi
        done
        cat /proc/net/sockstat /proc/net/sockstat6 /proc/loadavg
        awk '/^(MemTotal|MemAvailable|SwapFree):/' /proc/meminfo
        for name in nf_conntrack_count nf_conntrack_max; do
            if [ -r "/proc/sys/net/netfilter/$name" ]; then
                printf '%s=%s\n' "$name" "$(cat "/proc/sys/net/netfilter/$name")"
            fi
        done
    ) 2>/dev/null | head -c 7680 > "$rec_dir/incident.tmp" || return 1
    printf '%s\n' "$slot" > "$rec_dir/slot" &&
        mv -f "$rec_dir/incident.tmp" "$rec_incident"
}

recovery_rescue() {
    local marker_boot marker_time extra now
    [ -f "$rec_dir/pending" ] && [ ! -L "$rec_dir/pending" ] || return 1
    read -r marker_boot marker_time extra < "$rec_dir/pending" || return 1
    [ -n "$marker_boot" ] && [ -z "$extra" ] && recovery_uint "$marker_time" || return 1
    now=$(recovery_now) && recovery_uint "$now" || return 1
    if [ "$marker_boot" != "$rec_boot" ] || [ "$now" -lt "$marker_time" ] ||
        [ "$((now - marker_time))" -gt 180 ]; then
        rm -f "$rec_dir/pending" || return 1
        recovery_log 'pending=expired' || :
        return 2
    fi
    [ "$(recovery_setting airplane_mode_on)" = 0 ] || return 1
    case "$(recovery_setting mobile_data)" in
        0|1) recovery_phone enable || return 1;;
        *) return 1;;
    esac
    [ "$(recovery_setting mobile_data)" = 1 ] && rm -f "$rec_dir/pending"
}

recovery_redial() (
    [ ! -e "$rec_dir/pending" ] &&
        printf '%s %s\n' "$rec_boot" "$rec_now" > "$rec_dir/pending.tmp" &&
        mv -f "$rec_dir/pending.tmp" "$rec_dir/pending" || exit 2
    trap 'rm -f "$rec_dir/pending"' EXIT
    trap 'exit 1' HUP INT TERM
    # Make the budget and rescue marker durable before touching the radio.
    recovery_flush && recovery_allowed || exit 2
    trap 'if [ -e "$rec_dir/pending" ]; then recovery_rescue || :; fi' EXIT
    recovery_phone disable || exit 1
    sleep 4
    recovery_rescue
)

recovery_post() {
    local rec_deadline rec_wait result saw_failure=0
    rec_outcome=post-unavailable
    rec_deadline=$(recovery_now) && recovery_uint "$rec_deadline" || return 0
    rec_deadline=$((rec_deadline + 60))
    while rec_wait=$(recovery_remaining) && [ "$rec_wait" -gt 1 ]; do
        if recovery_allowed; then
            result=0
            recovery_probe || result=$?
            case "$result" in
                0) rec_outcome=post-healthy; return 0;;
                1) saw_failure=1;;
                3) break;;
                *) return 0;;
            esac
        elif [ "$?" = 3 ]; then
            break
        elif [ "$rec_reason" != no-cellular-uplink ]; then
            return 0
        fi
        rec_wait=$(recovery_remaining) || break
        [ "$rec_wait" -le 3 ] || rec_wait=3
        sleep "$rec_wait"
    done
    if [ "$saw_failure" = 1 ]; then rec_outcome=post-failed; fi
}

recovery_uint() {
    case "$1" in ''|*[!0-9]*|0[0-9]*) return 1;; esac
    [ "${#1}" -le 10 ]
}

recovery_load() {
    local saved_boot last streak extra n
    read -r saved_boot last streak a1 a2 a3 hold extra < "$rec_dir/state" || return 1
    [ -n "$saved_boot" ] && [ -z "$extra" ] || return 1
    for n in "$last" "$streak" "$a1" "$a2" "$a3" "$hold"; do recovery_uint "$n" || return 1; done
    [ "$a3" -le "$a2" ] && [ "$a2" -le "$a1" ] && [ "$a1" -le "$last" ] &&
        [ "$streak" -le 3 ] || return 1
    if [ "$saved_boot" != "$rec_boot" ]; then
        if [ "$hold" -gt "$last" ] || { [ "$a1" -ne 0 ] && [ "$((last - a1))" -lt 3600 ]; }; then
            hold=3600
        else
            hold=0
        fi
        a1=0 a2=0 a3=0
        return 0
    fi
    [ "$last" -le "$rec_now" ]
}

recovery_save() {
    # Keep the legacy streak field readable by the previous overlay.
    printf '%s %s 0 %s %s %s %s\n' "$rec_boot" "$rec_now" "$a1" "$a2" "$a3" "$hold" \
        > "$rec_dir/state.tmp" && mv -f "$rec_dir/state.tmp" "$rec_dir/state"
}

recovery_tick() (
    umask 077
    [ -f "$ROOT/recovery.enabled" ] || exit 0
    rec_dir=$ROOT/recovery
    mkdir -p "$rec_dir" && [ ! -L "$rec_dir" ] || exit 20
    rec_boot=$(recovery_boot) && rec_now=$(recovery_now) && recovery_uint "$rec_now" || exit 20
    [ -n "$rec_boot" ] || exit 20
    if [ -e "$rec_dir/pending" ]; then
        if recovery_rescue; then recovery_log 'pending=rescued' || :
        elif [ "$?" = 1 ]; then recovery_log 'pending=waiting' || :; fi
        exit 0
    fi
    [ -f "$ROOT/active" ] || exit 0
    a1=0 a2=0 a3=0 hold=0
    if [ -e "$rec_dir/state" ] && ! recovery_load; then
        a1=0 a2=0 a3=0 hold=$((rec_now + 3600))
        recovery_log 'state=reset budget=held' || :
    fi
    rec_reason=boot-grace
    rec_probes=
    recovery_save || exit 20
    for round in 1 2; do
        if [ "$rec_now" -lt 60 ] || ! recovery_allowed; then
            recovery_log "skipped=$rec_reason mobile_data=${rec_data:-unknown} airplane_mode=${rec_air:-unknown}" || :
            exit 0
        fi
        result=0
        recovery_probe || result=$?
        case "$result" in
            0) recovery_log "healthy $rec_probes" || :; exit 0;;
            1) recovery_log "failed=$round $rec_probes" || :;;
            *) recovery_log "probe-unavailable $rec_probes" || :; exit 0;;
        esac
        if [ "$round" = 1 ]; then
            if [ "$rec_now" -lt "$hold" ] ||
                { [ "$a1" -ne 0 ] && [ "$((rec_now - a1))" -lt 600 ]; } ||
                { [ "$a3" -ne 0 ] && [ "$((rec_now - a3))" -lt 3600 ]; }; then
                exit 0
            fi
            sleep 15
        fi
    done

    rec_incident=
    recovery_capture || { rec_incident=; recovery_log 'capture=unavailable' || :; }
    # Only the second attempt in this tick bypasses the cooldown.
    for attempt in 1 2; do
        attempt_now=$(recovery_now) && recovery_uint "$attempt_now" &&
            [ "$attempt_now" -ge "$rec_now" ] || exit 20
        rec_now=$attempt_now
        [ "$a3" -eq 0 ] || [ "$((rec_now - a3))" -ge 3600 ] || exit 0
        [ ! -e "$rec_dir/pending" ] && recovery_allowed || exit 0
        a3=$a2 a2=$a1 a1=$rec_now
        recovery_save || exit 20 # Spend the budget even if the attempt fails or is interrupted.
        rec_probes=
        rec_outcome=redial-failed
        result=0
        recovery_redial || result=$?
        if [ "$result" = 0 ]; then recovery_post; fi
        recovery_log "attempt=$rec_outcome $rec_probes" || :
        if [ -n "$rec_incident" ]; then
            printf 'attempt=%s %.230s\n' "$rec_outcome" "$rec_probes" >> "$rec_incident" || :
        fi
        case "$rec_outcome:$result" in post-failed:0|redial-failed:1) ;; *) break;; esac
    done
)
