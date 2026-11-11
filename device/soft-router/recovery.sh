# Internal functions; service.sh holds lifecycle.lock. Sourcing has no side effects.
recovery_now() { awk -F. 'NR == 1 {print $1}' /proc/uptime; }
recovery_boot() { cat /proc/sys/kernel/random/boot_id; }
recovery_timeout() { "${BB:-/data/adb/magisk/busybox}" timeout -k 2 5 "$@"; }
recovery_setting() { recovery_timeout /system/bin/settings get global "$1" 2>/dev/null; }
recovery_phone() { recovery_timeout /system/bin/cmd phone data "$1" >/dev/null 2>&1; }
recovery_flush() {
    recovery_timeout "${BB:-/data/adb/magisk/busybox}" sync "$rec_dir/state" "$rec_dir/pending" "$rec_dir" 2>/dev/null
}

recovery_allowed() {
    rec_reason=settings-unknown
    rec_data=$(recovery_setting mobile_data) || rec_data=unknown
    rec_air=$(recovery_setting airplane_mode_on) || rec_air=unknown
    case "$rec_data" in 0|1) ;; *) rec_data=unknown;; esac
    case "$rec_air" in 0|1) ;; *) rec_air=unknown;; esac
    case "$rec_data:$rec_air" in
        0:0) rec_reason=manual-off; return 1;;
        0:1|1:1) rec_reason=airplane-mode; return 1;;
        1:0) ;;
        *) return 1;;
    esac
    rec_reason=no-cellular-uplink
    rec_if=$(uplink 2>/dev/null) || return 1
    rec_reason=no-curl
    command -v curl >/dev/null
}

# HEAD gets the TLS and HTTP result without transferring or retaining a body.
# -q suppresses curlrc; if! forbids fallback to resolving the interface name.
recovery_probe() {
    rec_probes=
    rec_invalid=0
    for rec_host in www.baidu.com www.qq.com www.jd.com; do
        rec_rc=0
        rec_metrics=$(curl -q -4 --silent --noproxy '*' --interface "if!$rec_if" \
            --proto '=https' --head --output /dev/null --max-time 8 --connect-timeout 5 \
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
    # Recheck after diagnostics; don't take ownership of an already disabled radio.
    [ "$(recovery_setting mobile_data)" = 1 ] &&
        [ "$(recovery_setting airplane_mode_on)" = 0 ] || exit 1
    [ ! -e "$rec_dir/pending" ] &&
        printf '%s %s\n' "$rec_boot" "$rec_now" > "$rec_dir/pending.tmp" &&
        mv -f "$rec_dir/pending.tmp" "$rec_dir/pending" || exit 1
    # Make the budget and rescue marker durable before touching the radio.
    recovery_flush || { rm -f "$rec_dir/pending"; exit 1; }
    trap 'if [ -e "$rec_dir/pending" ]; then recovery_rescue || :; fi' EXIT
    trap 'exit 1' HUP INT TERM
    recovery_phone disable || exit 1
    sleep 4
    recovery_rescue
)

recovery_uint() {
    case "$1" in ''|*[!0-9]*|0[0-9]*) return 1;; esac
    [ "${#1}" -le 10 ]
}

recovery_load() {
    local saved_boot last extra n
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
        streak=0 a1=0 a2=0 a3=0
        return 0
    fi
    [ "$last" -le "$rec_now" ] || return 1
    # A gap can mean skipped observations (for example an update held the lock).
    if [ "$((rec_now - last))" -gt 150 ]; then streak=0; fi
}

recovery_save() {
    printf '%s %s %s %s %s %s %s\n' "$rec_boot" "$rec_now" "$streak" "$a1" "$a2" "$a3" "$hold" \
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
    streak=0 a1=0 a2=0 a3=0 hold=0
    if [ -e "$rec_dir/state" ] && ! recovery_load; then
        streak=0 a1=0 a2=0 a3=0 hold=$((rec_now + 3600))
        recovery_log 'state=reset budget=held' || :
    fi
    rec_reason=boot-grace
    if [ "$rec_now" -lt 300 ] || ! recovery_allowed; then
        streak=0
        recovery_save || exit 20
        recovery_log "skipped=$rec_reason mobile_data=${rec_data:-unknown} airplane_mode=${rec_air:-unknown}" || :
        exit 0
    fi
    rec_probes= rec_invalid=0
    result=0
    recovery_probe || result=$?
    if [ "$result" = 0 ] || [ "$rec_invalid" != 0 ]; then
        streak=0
        recovery_save || exit 20
        if [ "$result" = 0 ]; then recovery_log "healthy $rec_probes" || :
        else recovery_log "probe-unavailable $rec_probes" || :; fi
        exit 0
    fi
    [ "$streak" -ge 3 ] || streak=$((streak + 1))
    recovery_log "failed=$streak $rec_probes" || :
    if [ "$streak" -lt 3 ] || [ "$rec_now" -lt "$hold" ] ||
        { [ "$a1" -ne 0 ] && [ "$((rec_now - a1))" -lt 600 ]; } ||
        { [ "$a3" -ne 0 ] && [ "$((rec_now - a3))" -lt 3600 ]; }; then
        recovery_save
        exit $?
    fi

    rec_incident=
    recovery_capture || { rec_incident=; recovery_log 'capture=unavailable' || :; }
    attempt_now=$(recovery_now) && recovery_uint "$attempt_now" &&
        [ "$attempt_now" -ge "$rec_now" ] || exit 20
    rec_now=$attempt_now
    a3=$a2 a2=$a1 a1=$rec_now streak=0
    recovery_save || exit 20 # Spend the budget even if the attempt fails or is interrupted.
    rec_probes=
    rec_outcome=redial-failed
    if recovery_redial; then
        sleep 3
        rec_outcome=post-unavailable
        if recovery_allowed; then
            if recovery_probe; then rec_outcome=post-healthy
            elif [ "$rec_invalid" = 0 ]; then rec_outcome=post-failed; fi
        fi
    fi
    recovery_log "attempt=$rec_outcome $rec_probes" || :
    if [ -n "$rec_incident" ]; then
        printf 'attempt=%s %s\n' "$rec_outcome" "$rec_probes" | head -c 512 >> "$rec_incident" || :
    fi
)
