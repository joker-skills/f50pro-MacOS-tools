#!/system/bin/sh
# f50-soft-router-v1: software only; no watchdog or firmware operations.
ROOT=/data/adb/f50-soft-router
count=0
while [ -f "$ROOT/boot-enabled" ] && [ "$count" -lt 180 ]; do
    if [ "$(getprop sys.boot_completed)" = 1 ] &&
       ip -4 addr show dev br0 | grep -q 'inet 192.168.0.1/' &&
       ip -4 addr show dev sipa_eth8 | grep -q 'inet '; then
        if /data/adb/magisk/busybox ash "$ROOT/service.sh" boot >> "$ROOT/boot.log" 2>&1; then exit 0; fi
        /data/adb/magisk/busybox ash "$ROOT/service.sh" stop >> "$ROOT/boot.log" 2>&1
        exit 1
    fi
    count=$((count + 1))
    sleep 2
done
exit 1
