#!/system/bin/sh
# Runs as root at boot via Magisk service.d. Samples the USB gadget state for 6 minutes to catch the
# 3 s composition loop while the device is plugged into a host. One log per boot: /data/adb/f50mon-<bootid>.log
# NOTE: never rmdir /config/usb_gadget/g1/functions/accessory.gs2 on this kernel: it panics and reboots.
BOOTID=$(cut -c1-8 /proc/sys/kernel/random/boot_id 2>/dev/null)
LOG=/data/adb/f50mon-${BOOTID:-noid}.log
G=/config/usb_gadget/g1
echo "F50MON start $(date) uptime=$(cat /proc/uptime) bootid=$BOOTID" > $LOG
chmod 644 $LOG
last=""
n=0
while [ $n -lt 720 ]; do
  cfg="$(getprop sys.usb.config)|$(getprop sys.usb.state)|$(cat /sys/class/udc/musb-hdrc.1.auto/state 2>/dev/null)|udc=$(cat $G/UDC 2>/dev/null)|$(cat $G/idVendor 2>/dev/null):$(cat $G/idProduct 2>/dev/null)|$(ls $G/configs/b.1 2>/dev/null | grep -v -E 'MaxPower|bmAttributes|strings' | tr '\n' ',')"
  if [ "$cfg" != "$last" ]; then
    echo "$(cut -d' ' -f1 /proc/uptime) CHANGE $cfg" >> $LOG
    case "$cfg" in
      *accessory*) dumpsys usb 2>/dev/null | grep -iE 'accessory|manufacturer|model=|description|version=|uri=|serial' | head -12 >> $LOG ;;
    esac
    last="$cfg"
  fi
  n=$((n+1))
  sleep 0.5
done
echo "--- dmesg (tail 150) ---" >> $LOG
dmesg 2>/dev/null | grep -iE 'acc_|accessory|android_work|usb: |configfs|UDC|pullup' | tail -150 >> $LOG
echo "--- dumpsys usb ---" >> $LOG
dumpsys usb >> $LOG 2>&1
echo "F50MON end $(date)" >> $LOG
