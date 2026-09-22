#!/usr/bin/env bash
# Stage 2: read-only full backup of the F50 Pro (MU3356) over spd_dump, macOS native.
# Nothing is written to the device; only kick-to-download + reads + reset.
#
# Usage:  BACKUP_DIR=~/f50pro-backups/<tag> [KICK=2] [WAIT=300] bin/backup.sh
#   KICK: 2 = dl_diag (primary, same as the package's 一键Root.bat), 0 / 1 = the package's 备用2 / 备用3.
#
# Procedure (the device is bus-powered, so "powered off" simply means unplugged):
#   1. Unplug the F50 Pro from the Mac. The script refuses to start while it is still enumerated.
#   2. The script starts spd_dump and prints "Waiting for boot_diag/cali_diag/dl_diag connection".
#   3. Plug the F50 Pro straight into a Mac port (no hub). On cold power-up it shows up as a
#      Spreadtrum 1782:xxxx boot_diag device for a moment; spd_dump grabs it and kicks it into dl_diag.
#   4. Reads run; the device resets back to normal at the end.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPD_DUMP="${SPD_DUMP:-$ROOT/build/spd_dump}"
KICK="${KICK:-2}"
WAIT="${WAIT:-300}"
: "${BACKUP_DIR:?set BACKUP_DIR to a NEW directory, e.g. ~/f50pro-backups/b25-$(date +%Y%m%d)}"

die() { echo "error: $*" >&2; exit 1; }

[[ -x "$SPD_DUMP" ]] || die "spd_dump not built: run bin/build-spd_dump.sh"
[[ "$(id -u)" -ne 0 ]] || die "do not run as root: Apple Silicon USB accessory permission belongs to the console user"
[[ ! -e "$BACKUP_DIR" ]] || die "backup dir already exists: $BACKUP_DIR (use a new one; backups are never overwritten)"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

state="$(python3 "$ROOT/bin/usb-state.py" --absent-for 6 || true)"
[[ "$state" == "ABSENT" ]] || die "F50 Pro was seen on the bus within the last 6 s ($state). Unplug it first, then re-run."

echo "backup dir : $BACKUP_DIR"
echo "kick mode  : $KICK   wait: ${WAIT}s"
echo
echo ">>> Now plug the F50 Pro directly into the Mac. <<<"
echo

# r all = every partition except blackbox/cache/userdata, both A/B slots (r all_lite would skip the inactive slot).
"$SPD_DUMP" --wait "$WAIT" --kickto "$KICK" --verbose 1 \
  path "$BACKUP_DIR" \
  partition_list "$BACKUP_DIR/partition_list.xml" \
  r all \
  reset 2>&1 | tee "$BACKUP_DIR/spd_dump.log"

echo
echo "---- verification ----"
cd "$BACKUP_DIR"
shasum -a 256 -- *.bin *.img *.xml 2>/dev/null | tee SHA256SUMS >/dev/null || true
/bin/ls -l | awk 'NR>1{printf "%12s  %s\n",$5,$9}'

python3 - "$BACKUP_DIR" <<'PY'
import glob, os, sys
d = sys.argv[1]
problems = []
ib = sorted(glob.glob(os.path.join(d, "init_boot*.bin")) + glob.glob(os.path.join(d, "init_boot*.img")))
tos = sorted(glob.glob(os.path.join(d, "trustos*.bin")) + glob.glob(os.path.join(d, "trustos*.img")))
if not ib: problems.append("no init_boot dump")
if not tos: problems.append("no trustos dump")
for p in ib:
    b = open(p, "rb").read()
    if b[:8] != b"ANDROID!": problems.append(f"{os.path.basename(p)}: not an Android bootimg")
    i = b.find(b"ZTE/MU3356/")
    print(os.path.basename(p), len(b), "bytes", "fingerprint:", b[i:i+90].split(b"\0")[0].decode("ascii", "replace") if i >= 0 else "NOT FOUND")
for p in tos:
    b = open(p, "rb").read()
    if b[:4] != b"DHTB": problems.append(f"{os.path.basename(p)}: trustos missing DHTB header")
    print(os.path.basename(p), len(b), "bytes")
# completeness against the device's own partition table (everything except userdata/cache/blackbox)
import re
xml_path = os.path.join(d, "partition_list.xml")
if os.path.exists(xml_path):
    xml = open(xml_path, encoding="utf-8", errors="replace").read()
    names = re.findall(r'id="([^"]+)"', xml) or re.findall(r'name="([^"]+)"', xml)
    have = {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(d, "*.bin"))}
    for n in names:
        if n not in have and n not in ("userdata", "cache", "blackbox"):
            problems.append(f"partition in table but not dumped: {n}")
    print("partition table entries:", len(names), "dumped:", len(have))
else:
    problems.append("partition_list.xml missing (fdl2 did not return a partition table)")
if problems:
    print("BACKUP INCOMPLETE:"); [print("  -", x) for x in problems]; sys.exit(1)
print("backup looks complete")
PY
echo "Backup stored in $BACKUP_DIR. Keep it; it is the only rollback for this device."
