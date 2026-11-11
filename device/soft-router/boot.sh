#!/system/bin/sh
# f50-soft-router-v1: software only; no watchdog or firmware operations.
ROOT=/data/adb/f50-soft-router
service() { /data/adb/magisk/busybox ash "$ROOT/service.sh" "$@"; }
count=0
while [ -f "$ROOT/boot-enabled" ] && [ "$count" -lt 180 ]; do
    if [ "$(/system/bin/getprop sys.boot_completed)" = 1 ]; then
        if [ -f "$ROOT/recovery.enabled" ] && [ -f "$ROOT/recovery/pending" ]; then
            service recovery-rescue || :
        fi
        if /system/bin/ip -4 addr show dev br0 | grep -q 'inet 192.168.0.1/' &&
           service uplink >/dev/null 2>&1; then
            service boot >> "$ROOT/boot.log" 2>&1
            result=$?
            [ "$result" = 0 ] && exit 0
            [ "$result" = 28 ] && exit 28
            service stop >> "$ROOT/boot.log" 2>&1
            exit 1
        fi
    fi
    count=$((count + 1))
    sleep 2
done
exit 1
