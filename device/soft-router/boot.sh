#!/system/bin/sh
# f50-soft-router-v1: software only; no watchdog or firmware operations.
ROOT=/data/adb/f50-soft-router
count=0
while [ -f "$ROOT/boot-enabled" ] && [ "$count" -lt 180 ]; do
    if [ "$(/system/bin/getprop sys.boot_completed)" = 1 ] &&
       /system/bin/ip -4 addr show dev br0 | grep -q 'inet 192.168.0.1/' &&
       /data/adb/magisk/busybox ash "$ROOT/service.sh" uplink >/dev/null 2>&1; then
        /data/adb/magisk/busybox ash "$ROOT/service.sh" boot >> "$ROOT/boot.log" 2>&1
        result=$?
        [ "$result" = 0 ] && exit 0
        [ "$result" = 28 ] && exit 28
        /data/adb/magisk/busybox ash "$ROOT/service.sh" stop >> "$ROOT/boot.log" 2>&1
        exit 1
    fi
    count=$((count + 1))
    sleep 2
done
exit 1
