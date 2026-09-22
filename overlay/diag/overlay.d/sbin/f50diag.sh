#!/system/bin/sh
# f50pro-tools: enable ADB the way Android expects (adb_enabled setting), restart adbd on tcp:5555,
# dump USB/ADB diagnostics to /data/local/tmp/f50diag.txt and mirror them into the sd_klog partition
# for offline readback via spd_dump. Runs once per boot from Magisk overlay.d.
LOG=/data/local/tmp/f50diag.txt
mkdir -p /data/local/tmp
echo "F50DIAG start $(date)" > $LOG
chmod 666 $LOG
setprop sys.f50diag.started 1

# wait for system_server (settings provider) - up to 180 s
i=0
while [ "$(getprop sys.boot_completed)" != "1" ] && [ $i -lt 90 ]; do sleep 2; i=$((i+1)); done
echo "boot_completed=$(getprop sys.boot_completed) after ${i}x2s" >> $LOG

enable_adb() {
  settings put global adb_enabled 1 2>>$LOG
  settings put global adb_wifi_enabled 1 2>>$LOG
  settings put global development_settings_enabled 1 2>>$LOG
  setprop persist.service.adb.enable 1
  setprop service.adb.tcp.port 5555
  setprop persist.adb.tcp.port 5555
  stop adbd
  sleep 1
  start adbd
}

diag() {
  echo "===== $(date) diag pass $1 =====" >> $LOG
  echo "--- id / build ---" >> $LOG
  id >> $LOG 2>&1
  getprop ro.build.fingerprint >> $LOG 2>&1
  echo "--- settings ---" >> $LOG
  for k in adb_enabled adb_wifi_enabled development_settings_enabled; do echo "$k=$(settings get global $k 2>&1)" >> $LOG; done
  echo "--- props (usb/adb/magisk/zte usb) ---" >> $LOG
  getprop | grep -iE 'usb|adb|magisk|kano|f50diag|zte.*(usb|mode)|sys.boot_completed|ro.debuggable|ro.secure' >> $LOG 2>&1
  echo "--- adbd binary / processes ---" >> $LOG
  ls -la /apex/com.android.adbd/bin/ >> $LOG 2>&1
  ps -A -o PID,USER,NAME 2>/dev/null | grep -iE 'adbd|usb|zte|switch|hw_usb' >> $LOG 2>&1
  echo "--- listening sockets ---" >> $LOG
  netstat -tln >> $LOG 2>&1
  echo "--- configfs gadget ---" >> $LOG
  for g in /config/usb_gadget/*; do
    [ -d "$g" ] || continue
    echo "== $g UDC=$(cat $g/UDC 2>&1) vid=$(cat $g/idVendor 2>&1) pid=$(cat $g/idProduct 2>&1)" >> $LOG
    ls -la $g/functions/ >> $LOG 2>&1
    for c in $g/configs/*; do echo "-- $c"; ls -la $c/ 2>&1; done >> $LOG 2>&1
  done
  echo "--- android_usb legacy ---" >> $LOG
  cat /sys/class/android_usb/android0/state /sys/class/android_usb/android0/functions >> $LOG 2>&1
  ls /sys/class/udc/ >> $LOG 2>&1
  for u in /sys/class/udc/*; do echo "$u state=$(cat $u/state 2>&1) speed=$(cat $u/current_speed 2>&1)" >> $LOG; done
  echo "--- zte usb scripts ---" >> $LOG
  ls -la /vendor/bin/*usb* /system/bin/*usb* /vendor/etc/init/*usb* /system/etc/init/*usb* >> $LOG 2>&1
  [ -f /vendor/bin/switch_usb_mode.sh ] && { echo "== /vendor/bin/switch_usb_mode.sh"; cat /vendor/bin/switch_usb_mode.sh; } >> $LOG 2>&1
  echo "--- logcat (usb/adb/gadget, last 400) ---" >> $LOG
  logcat -d 2>/dev/null | grep -iE 'usb|adb|gadget|rndis|ncm|mtp' | tail -400 >> $LOG 2>&1
  echo "--- dmesg (usb, last 300) ---" >> $LOG
  dmesg 2>&1 | grep -iE 'usb|gadget|dwc|udc|configfs|musb' | tail -300 >> $LOG 2>&1
  chmod 666 $LOG
}

enable_adb
sleep 8
diag 1

sleep 60
enable_adb
sleep 8
diag 2

# offline readback channel: mirror the log into the sd_klog partition (10 MiB kernel-log dump area, backed up in stage 2)
# so it can be read with spd_dump `r sd_klog` from download mode even if adbd never comes up.
KLOG=/dev/block/by-name/sd_klog
if [ -e "$KLOG" ]; then
  { echo "F50DIAGLOG"; cat $LOG; echo "F50DIAGEND"; } | dd of=$KLOG bs=4096 conv=sync 2>>$LOG
  echo "log mirrored to sd_klog rc=$?" >> $LOG
fi
exit 0
