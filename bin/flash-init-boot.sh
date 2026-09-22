#!/usr/bin/env bash
# Write ONE init_boot image to the active slot (w_force init_boot <IMG> reset). Same kick route as flash-root.sh.
# Usage:  IMG=/abs/path/init_boot.img BACKUP_DIR=~/f50pro-backups/<tag> CONFIRM=FLASH [KICK=2] [WAIT=300] bin/flash-init-boot.sh
#         DRY_RUN=1 IMG=... BACKUP_DIR=... bin/flash-init-boot.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPD_DUMP="${SPD_DUMP:-$ROOT/build/spd_dump}"
KICK="${KICK:-2}"
WAIT="${WAIT:-300}"
DRY_RUN="${DRY_RUN:-0}"
: "${IMG:?set IMG to the init_boot image to write}"
: "${BACKUP_DIR:?set BACKUP_DIR to the completed stage-2 backup (rollback source)}"
if [[ "$DRY_RUN" != "1" ]]; then
  : "${CONFIRM:?set CONFIRM=FLASH}"
  [[ "$CONFIRM" == "FLASH" ]] || { echo "CONFIRM must be exactly FLASH" >&2; exit 1; }
fi
die() { echo "error: $*" >&2; exit 1; }

[[ -x "$SPD_DUMP" ]] || die "spd_dump not built"
[[ "$(id -u)" -ne 0 ]] || die "do not run as root"
[[ -f "$IMG" ]] || die "image missing: $IMG"
IMG="$(cd "$(dirname "$IMG")" && pwd)/$(basename "$IMG")"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)" || die "backup dir missing"
[[ -f "$BACKUP_DIR/init_boot_a.bin" && -f "$BACKUP_DIR/trustos_a.bin" ]] || die "backup lacks init_boot_a/trustos_a"

python3 - "$IMG" "$BACKUP_DIR" <<'PY'
import struct, sys, os
img = open(sys.argv[1], "rb").read()
if img[:8] != b"ANDROID!": sys.exit("not an Android bootimg")
ks, rs = struct.unpack("<II", img[8:16]); hv = struct.unpack("<I", img[40:44])[0]
if ks != 0 or hv != 4: sys.exit(f"unexpected header: kernel_size={ks} header_version={hv}")
stock = open(os.path.join(sys.argv[2], "init_boot_a.bin"), "rb").read()
if len(img) != len(stock): sys.exit(f"image size {len(img)} != partition image size {len(stock)}")
def fp(b):
    i = b.find(b"ZTE/MU3356/"); return b[i:].split(b"\0",1)[0].split()[0].decode() if i >= 0 else None
print("image ramdisk_size", rs, "fingerprint", fp(img))
if fp(img) != fp(stock): sys.exit("fingerprint differs from the stock active-slot init_boot; refusing")
print("ok")
PY
echo "sha256: $(shasum -a 256 "$IMG" | cut -c1-64)"

if [[ "$DRY_RUN" == "1" ]]; then echo "DRY_RUN=1: pre-checks passed, stopping before spd_dump."; exit 0; fi

state="$(python3 "$ROOT/bin/usb-state.py" --absent-for 6 || true)"
[[ "$state" == "ABSENT" ]] || die "F50 Pro was seen on the bus within the last 6 s ($state). Unplug it first."

LOG="$BACKUP_DIR/flash-init-boot-$(date +%Y%m%d-%H%M%S).log"
echo "About to write init_boot (active slot) <- $IMG"
echo ">>> Now plug the F50 Pro directly into the Mac. <<<"
set +e
"$SPD_DUMP" --wait "$WAIT" --kickto "$KICK" --verbose 0 path "$(dirname "$IMG")" w_force init_boot "$IMG" reset 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
done_count=$(grep -cE 'Write Part Done|Force Write' "$LOG" || true)
echo "spd_dump exit: $rc   write markers: $done_count (need 1)"
[[ "$done_count" -ge 1 ]] && echo "WRITE OK" || { echo "WRITE NOT CONFIRMED, read $LOG; rollback: bin/restore.sh" >&2; exit 1; }
