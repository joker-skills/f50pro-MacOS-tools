#!/system/bin/sh
# f50pro-MacOS-tools (Magisk post-fs-data.d): hide the android.hardware.usb.accessory system feature.
# UsbDeviceManager.startAccessoryMode() returns immediately when the feature is absent, so AOA probes
# from hosts (Mac WeChat sends GETPROTOCOL/SENDSTRING/START to every new Android-looking USB device)
# no longer switch the gadget into accessory mode. That switch was the 3 s re-enumeration loop.
# Runtime-only bind mount, re-applied every boot; delete this file to undo.
D=/data/adb/f50
T=/vendor/etc/permissions/android.hardware.usb.accessory.xml
mkdir -p $D
printf '<?xml version="1.0" encoding="utf-8"?>\n<permissions>\n</permissions>\n' > $D/empty-permissions.xml
chmod 644 $D/empty-permissions.xml
chcon u:object_r:vendor_configs_file:s0 $D/empty-permissions.xml 2>/dev/null
if mount -o bind $D/empty-permissions.xml $T; then
  echo "$(date) bind ok: $T" > $D/no-aoa.log
else
  echo "$(date) bind FAILED rc=$?" > $D/no-aoa.log
fi
