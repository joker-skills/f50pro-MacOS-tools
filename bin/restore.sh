#!/usr/bin/env bash
# Rollback: write the stage-2 stock trustos and init_boot of the active slot back to the device.
# Usage:  BACKUP_DIR=~/f50pro-backups/<tag> CONFIRM=RESTORE [KICK=2] [WAIT=300] bin/restore.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPD_DUMP="${SPD_DUMP:-$ROOT/build/spd_dump}"
KICK="${KICK:-2}"
WAIT="${WAIT:-300}"
: "${BACKUP_DIR:?set BACKUP_DIR to the stage-2 backup}"
: "${CONFIRM:?set CONFIRM=RESTORE}"
[[ "$CONFIRM" == "RESTORE" ]] || { echo "CONFIRM must be exactly RESTORE" >&2; exit 1; }
die() { echo "error: $*" >&2; exit 1; }

[[ -x "$SPD_DUMP" ]] || die "spd_dump not built"
[[ "$(id -u)" -ne 0 ]] || die "do not run as root"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)" || die "backup dir missing"

slot="$(python3 - "$BACKUP_DIR" <<'PY'
import sys, os
misc = open(os.path.join(sys.argv[1], "misc.bin"), "rb").read()
s = misc[2048:2052].split(b"\0")[0].decode()
print(s if s in ("_a", "_b") else "")
PY
)"
[[ -n "$slot" ]] || die "cannot determine active slot from backup misc.bin"
TOS="$BACKUP_DIR/trustos$slot.bin"; IB="$BACKUP_DIR/init_boot$slot.bin"
[[ -f "$TOS" && -f "$IB" ]] || die "backup images for slot $slot missing"
grep -q "  $(basename "$TOS")\$" "$BACKUP_DIR/SHA256SUMS" && (cd "$BACKUP_DIR" && shasum -a 256 -c --quiet <(grep -E "trustos$slot.bin|init_boot$slot.bin" SHA256SUMS)) || die "backup image checksum mismatch"

state="$(python3 "$ROOT/bin/usb-state.py" --absent-for 6 || true)"
[[ "$state" == "ABSENT" ]] || die "F50 Pro was seen on the bus within the last 6 s ($state). Unplug it first."

LOG="$BACKUP_DIR/restore-$(date +%Y%m%d-%H%M%S).log"
echo "restoring slot $slot: trustos$slot <- $TOS ; init_boot$slot <- $IB"
echo ">>> Now plug the F50 Pro directly into the Mac. <<<"
set +e
"$SPD_DUMP" --wait "$WAIT" --kickto "$KICK" --verbose 0 \
  path "$BACKUP_DIR" \
  w "trustos$slot" "$TOS" \
  w_force "init_boot$slot" "$IB" \
  reset 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
done_count=$(grep -cE 'Write Part Done|Force Write' "$LOG" || true)
echo "spd_dump exit: $rc   write markers: $done_count (need 2)"
[[ "$done_count" -ge 2 ]] && echo "RESTORE OK" || { echo "RESTORE NOT CONFIRMED, read $LOG" >&2; exit 1; }
