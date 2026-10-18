#!/data/adb/magisk/busybox ash
set -eu
ROOT=/data/adb/f50-soft-router
uplink() {
    routes=$(/system/bin/ip -4 route show table all) || return 29
    addresses=$(/system/bin/ip -4 addr show) || return 29
    printf '%s\n' "$routes" '--F50-ADDRESSES--' "$addresses" | awk '
        $0 == "--F50-ADDRESSES--" { addresses=1; next }
        !addresses && $1 == "default" && $0 !~ /linkdown/ {
            for (i=1; i<NF; i++)
                if ($i == "dev" && $(i+1) ~ /^sipa_eth[0-9]+$/) route[$(i+1)]=1
        }
        addresses && /^[0-9]+:/ {
            dev=$2; sub(/:.*/, "", dev); sub(/@.*/, "", dev)
            up=($0 ~ /<([^>]*,)?UP(,|>)/)
        }
        addresses && up && $1 == "inet" && $2 !~ /^0\.0\.0\.0\// { ipv4[dev]=1 }
        END {
            for (dev in route) if (ipv4[dev]) { selected=dev; count++ }
            if (count != 1) exit 29
            print selected
        }'
}
if [ "${1:-}" = uplink ]; then
    uplink
    exit $?
fi
idle() {
    for exe in /proc/[0-9]*/exe; do
        target=$(readlink "$exe" 2>/dev/null || true)
        case "$target" in "$ROOT/mihomo"|"$ROOT/tailscaled") return 1;; esac
    done
}
if [ "${1:-}" = idle ]; then
    idle && [ ! -e "$ROOT/active" ]
    exit $?
fi
cd "$ROOT"
[ "$(cat owner)" = f50-soft-router-v1 ] || exit 20
umask 077
exec 9> "$ROOT/lifecycle.lock"
/data/adb/magisk/busybox flock -n 9 || exit 28

# Only the host-generated top-level field may change; nested policy stays byte-for-byte.
render() {
    awk -v interface="$1" '
        /^  "interface-name": "__F50_CELLULAR_INTERFACE__"[,]?$/ {
            sub(/__F50_CELLULAR_INTERFACE__/, interface); count++
        }
        { print }
        END { if (count != 1) exit 29 }' "${2:-config.json}"
}

# PID plus executable plus kernel start time avoids signaling a recycled PID.
owned() {
    [ -f "$1.pid" ] || return 1
    read pid birth < "$1.pid"
    case "$pid:$birth" in *[!0-9:]*|:*) return 2;; esac
    [ -r "/proc/$pid/stat" ] || return 1
    [ "$(readlink /proc/$pid/exe)" = "$ROOT/$1" ] || return 2
    [ "$(awk '{print $22}' /proc/$pid/stat)" = "$birth" ] || return 2
}
record() {
    pid=$2
    echo "$pid $(awk '{print $22}' /proc/$pid/stat)" > "$1.pid"
    sleep 1
    owned "$1" || exit 21
}
# UPDATE_FUNCTIONS
if ! command -v updates_install >/dev/null 2>&1 && [ -f "$ROOT/updates.sh" ]; then
    . "$ROOT/updates.sh"
fi
action=${1:-}
if [ "$action" = boot ]; then
    [ -f boot-enabled ] || exit 0
    if owned mihomo && owned tailscaled; then
        if [ -f updates.enabled ]; then schedule || exit 28; fi
        exit 0
    fi
    idle || exit 24
    rm -f active mihomo.pid tailscaled.pid
    action=start
fi
case "$action" in
updates-install) updates_install;;
updates-check) updates_check;;
updates-remove) updates_remove;;
update) update_config;;
start)
    [ ! -e active ] || exit 22
    for name in mihomo tailscaled; do
        if owned "$name"; then exit 22; else code=$?; [ "$code" = 1 ] || exit 23; fi
    done
    trap 'rm -f "$ROOT/runtime.tmp"' EXIT
    rm -f runtime.tmp
    interface=$(uplink) || exit 29
    render "$interface" > runtime.tmp || exit 29
    mv runtime.tmp runtime.json
    "$ROOT/mihomo" -t -d "$ROOT" -f "$ROOT/runtime.json" >/dev/null 2>&1 || exit 29
    touch active
    if [ "$(cat mode)" = transparent ]; then /system/bin/sh "$ROOT/network.sh" prepare; fi
    (exec 9>&-; trap '' HUP; exec "$ROOT/mihomo" -d "$ROOT" -f "$ROOT/runtime.json") >mihomo.log 2>&1 </dev/null &
    record mihomo $!
    (exec 9>&-; trap '' HUP; exec "$ROOT/tailscaled" --tun=userspace-networking --socket="$ROOT/tailscaled.sock" --state="$ROOT/tailscale.state") >tailscaled.log 2>&1 </dev/null &
    record tailscaled $!
    if [ "$(cat mode)" = transparent ]; then /system/bin/sh "$ROOT/network.sh" enable; fi
    if [ -f updates.enabled ]; then resume_updates; fi
    ;;
check)
    if [ ! -e active ]; then
        idle || exit 24
        for name in mihomo tailscaled; do
            if owned "$name"; then exit 24; else code=$?; [ "$code" = 1 ] || exit 23; fi
        done
        exit 10
    fi
    owned mihomo && owned tailscaled || exit 25
    if [ -f updates.enabled ] || [ -e updates.backup ]; then updates_check || exit 20; fi
    interface=$(uplink) || exit 29
    expected=$(render "$interface") || exit 29
    expected_sha=$(printf '%s\n' "$expected" | sha256sum) || exit 29
    actual_sha=$(sha256sum runtime.json) || exit 29
    [ "${expected_sha%% *}" = "${actual_sha%% *}" ] || exit 29
    if [ "$(cat mode)" = transparent ]; then /system/bin/sh "$ROOT/network.sh" check; fi
    ;;
stop)
    if [ -f network-owned ]; then /system/bin/sh "$ROOT/network.sh" unhook; fi
    for name in mihomo tailscaled; do
        if owned "$name"; then
            kill "$pid"
            count=0
            while owned "$name"; do
                count=$((count + 1))
                [ "$count" -lt 15 ] || exit 26
                sleep 1
            done
        fi
    done
    idle || exit 27
    if [ -f network-owned ]; then /system/bin/sh "$ROOT/network.sh" cleanup; fi
    rm -f active mihomo.pid tailscaled.pid runtime.json runtime.tmp
    ;;
*) exit 2;;
esac
