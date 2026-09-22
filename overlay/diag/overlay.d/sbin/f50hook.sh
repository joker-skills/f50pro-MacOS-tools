#!/system/bin/sh
# f50pro-tools boot hook (root, from Magisk overlay.d):
#  1. grant Magisk su to the adb shell uid (2000) so `adb shell su` works without the Magisk app UI
#  2. run /data/local/tmp/f50hook.sh if present (iterate diagnostics without reflashing init_boot)
LOG=/data/local/tmp/f50hook.log
mkdir -p /data/local/tmp
echo "F50HOOK start $(date)" > $LOG
chmod 666 $LOG
MAGISK=/debug_ramdisk/magisk
[ -x "$MAGISK" ] || MAGISK="$(dirname "$0")/magisk"
i=0
while [ ! -f /data/adb/magisk.db ] && [ $i -lt 60 ]; do sleep 2; i=$((i+1)); done
if [ -x "$MAGISK" ]; then
  "$MAGISK" --sqlite "INSERT OR REPLACE INTO policies (uid,policy,until,logging,notification) VALUES (2000,2,0,1,0);" >> $LOG 2>&1
  echo "policy rows: $("$MAGISK" --sqlite 'SELECT uid,policy FROM policies;' 2>&1)" >> $LOG
else
  echo "magisk binary not found" >> $LOG
fi
if [ -f /data/local/tmp/f50hook.sh ]; then
  echo "running /data/local/tmp/f50hook.sh" >> $LOG
  sh /data/local/tmp/f50hook.sh >> $LOG 2>&1 &
fi
exit 0
