#!/system/bin/sh
set -eu
ROOT=/data/adb/f50-soft-router
cd "$ROOT"
[ "$(cat owner)" = f50-soft-router-v1 ] || exit 20
umask 077

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
idle() {
    for exe in /proc/[0-9]*/exe; do
        target=$(readlink "$exe" 2>/dev/null || true)
        case "$target" in "$ROOT/mihomo"|"$ROOT/tailscaled") return 1;; esac
    done
}
case "${1:-}" in
idle) idle && [ ! -e active ] ;;
start)
    [ ! -e active ] || exit 22
    for name in mihomo tailscaled; do
        if owned "$name"; then exit 22; else code=$?; [ "$code" = 1 ] || exit 23; fi
    done
    touch active
    (trap '' HUP; exec "$ROOT/mihomo" -d "$ROOT" -f "$ROOT/config.json") >mihomo.log 2>&1 </dev/null &
    record mihomo $!
    (trap '' HUP; exec "$ROOT/tailscaled" --tun=userspace-networking --socket="$ROOT/tailscaled.sock" --state="$ROOT/tailscale.state") >tailscaled.log 2>&1 </dev/null &
    record tailscaled $!
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
    ;;
stop)
    for name in mihomo tailscaled; do
        if owned "$name"; then
            kill "$pid"
            count=0
            while owned "$name"; do
                count=$((count + 1))
                [ "$count" -lt 15 ] || exit 26
                sleep 1
            done
        else
            code=$?; [ "$code" = 1 ] || exit 23
        fi
    done
    idle || exit 27
    rm -f active mihomo.pid tailscaled.pid
    ;;
*) exit 2;;
esac
