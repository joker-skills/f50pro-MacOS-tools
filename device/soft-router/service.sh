#!/system/bin/sh
set -eu
ROOT=/data/adb/f50-soft-router
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
action=${1:-}
if [ "$action" = boot ]; then
    [ -f boot-enabled ] || exit 0
    if owned mihomo && owned tailscaled; then exit 0; fi
    idle || exit 24
    rm -f active mihomo.pid tailscaled.pid
    action=start
fi
case "$action" in
start)
    [ ! -e active ] || exit 22
    for name in mihomo tailscaled; do
        if owned "$name"; then exit 22; else code=$?; [ "$code" = 1 ] || exit 23; fi
    done
    touch active
    if [ "$(cat mode)" = transparent ]; then sh "$ROOT/network.sh" prepare; fi
    (exec 9>&-; trap '' HUP; exec "$ROOT/mihomo" -d "$ROOT" -f "$ROOT/config.json") >mihomo.log 2>&1 </dev/null &
    record mihomo $!
    (exec 9>&-; trap '' HUP; exec "$ROOT/tailscaled" --tun=userspace-networking --socket="$ROOT/tailscaled.sock" --state="$ROOT/tailscale.state") >tailscaled.log 2>&1 </dev/null &
    record tailscaled $!
    if [ "$(cat mode)" = transparent ]; then sh "$ROOT/network.sh" enable; fi
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
    if [ "$(cat mode)" = transparent ]; then sh "$ROOT/network.sh" check; fi
    ;;
stop)
    if [ -f network-owned ]; then sh "$ROOT/network.sh" unhook; fi
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
    if [ -f network-owned ]; then sh "$ROOT/network.sh" cleanup; fi
    rm -f active mihomo.pid tailscaled.pid
    ;;
*) exit 2;;
esac
