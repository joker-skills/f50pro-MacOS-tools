#!/usr/bin/env bash
# Stage 3: flash the UFI-TOOLS author's B25 root package (trustos + init_boot on the active slot).
# IRREVERSIBLE WRITE. Mirrors the package's [直接插电]一键Root.bat but with guardrails.
#
# Usage:  BACKUP_DIR=~/f50pro-backups/<tag> CONFIRM=FLASH [KICK=2] [WAIT=300] bin/flash-root.sh
#         DRY_RUN=1 BACKUP_DIR=... bin/flash-root.sh   -> run every pre-check and stop before spd_dump
#
# Guardrails (any failure aborts before touching the device):
#   - a complete stage-2 backup exists (partition table vs dumps, init_boot/trustos headers)
#   - package payloads match the audited sha256 (B25 package, 2026-09-22)
#   - the ACTIVE slot's stock init_boot fingerprint equals the package magisk.img fingerprint (same build)
#   - device is unplugged when we start (we need the cold power-up boot_diag window)
#   - success is judged only by two "Write Part Done"/"Force Write" markers; EXIT:0 alone is not success
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPD_DUMP="${SPD_DUMP:-$ROOT/build/spd_dump}"
PKG_BIN="${PKG_BIN:-$ROOT/pkg/B25/bin}"
KICK="${KICK:-2}"
WAIT="${WAIT:-300}"
: "${BACKUP_DIR:?set BACKUP_DIR to the completed stage-2 backup}"
DRY_RUN="${DRY_RUN:-0}"
if [[ "$DRY_RUN" != "1" ]]; then
  : "${CONFIRM:?set CONFIRM=FLASH to acknowledge this writes trustos and init_boot}"
  [[ "$CONFIRM" == "FLASH" ]] || { echo "CONFIRM must be exactly FLASH" >&2; exit 1; }
fi

SHA_MAGISK="bfb78556b15e21ef54368169d414437cd710297139650cad4e4ac12bc2af1262"
SHA_TOS="cc479a48cabc565a9772434902c10b22cde9d6f3271db909f4783d9f96da0a01"

die() { echo "error: $*" >&2; exit 1; }

[[ -x "$SPD_DUMP" ]] || die "spd_dump not built"
[[ "$(id -u)" -ne 0 ]] || die "do not run as root"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)" || die "backup dir missing"
PKG_BIN="$(cd "$PKG_BIN" && pwd)" || die "package bin dir missing"
TOS="$PKG_BIN/tos.bin"; MAGISK="$PKG_BIN/magisk.img"
[[ -f "$TOS" && -f "$MAGISK" ]] || die "tos.bin / magisk.img missing in $PKG_BIN"

echo "== payload hashes =="
[[ "$(shasum -a 256 "$TOS" | cut -c1-64)" == "$SHA_TOS" ]] || die "tos.bin sha256 differs from the audited B25 package"
[[ "$(shasum -a 256 "$MAGISK" | cut -c1-64)" == "$SHA_MAGISK" ]] || die "magisk.img sha256 differs from the audited B25 package"
echo "ok"

echo "== backup completeness =="
python3 - "$BACKUP_DIR" "$MAGISK" <<'PY'
import glob, os, re, sys
d, magisk = sys.argv[1], sys.argv[2]
xml = os.path.join(d, "partition_list.xml")
if not os.path.exists(xml): sys.exit("partition_list.xml missing in backup")
names = re.findall(r'id="([^"]+)"', open(xml, encoding="utf-8", errors="replace").read())
have = {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(d, "*.bin"))}
missing = [n for n in names if n not in have and n not in ("userdata", "cache", "blackbox")]
if missing: sys.exit(f"backup incomplete, missing: {missing}")
misc = open(os.path.join(d, "misc.bin"), "rb").read()
suffix = misc[2048:2052].split(b"\0")[0].decode()
if suffix not in ("_a", "_b"): sys.exit(f"cannot read active slot from misc.bin: {suffix!r}")
ib = open(os.path.join(d, f"init_boot{suffix}.bin"), "rb").read()
tos = open(os.path.join(d, f"trustos{suffix}.bin"), "rb").read()
if ib[:8] != b"ANDROID!" or tos[:4] != b"DHTB": sys.exit("backup init_boot/trustos headers wrong")
def fp(b):
    i = b.find(b"ZTE/MU3356/")
    return b[i:].split(b"\0", 1)[0].split()[0].decode() if i >= 0 else None
stock, pkg = fp(ib), fp(open(magisk, "rb").read())
print("active slot      :", suffix)
print("stock fingerprint:", stock)
print("pkg   fingerprint:", pkg)
if not stock or stock != pkg: sys.exit("fingerprint mismatch: package was built for a different firmware build; DO NOT FLASH")
print("ok: package matches the active slot's firmware build")
PY

if [[ "$DRY_RUN" == "1" ]]; then echo "DRY_RUN=1: all pre-checks passed, stopping before spd_dump."; exit 0; fi

state="$(python3 "$ROOT/bin/usb-state.py" --absent-for 6 || true)"
[[ "$state" == "ABSENT" ]] || die "F50 Pro was seen on the bus within the last 6 s ($state). Unplug it first."

LOG="$BACKUP_DIR/flash-root-$(date +%Y%m%d-%H%M%S).log"
echo
echo "About to run (writes the ACTIVE slot's trustos and init_boot):"
echo "  spd_dump --wait $WAIT --kickto $KICK path $PKG_BIN w trustos $TOS w_force init_boot $MAGISK reset"
echo "log: $LOG"
echo
echo ">>> Now plug the F50 Pro directly into the Mac. <<<"
echo

set +e
"$SPD_DUMP" --wait "$WAIT" --kickto "$KICK" --verbose 0 \
  path "$PKG_BIN" \
  w trustos "$TOS" \
  w_force init_boot "$MAGISK" \
  reset 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e

echo
echo "---- result ----"
done_count=$(grep -cE 'Write Part Done|Force Write' "$LOG" || true)
echo "spd_dump exit: $rc   write markers: $done_count (need 2)"
if [[ "$done_count" -ge 2 ]]; then
  echo "FLASH OK. After it boots, reconnect Wi-Fi to the F50 and run: adb connect 192.168.0.1:5555"
else
  echo "FLASH NOT CONFIRMED. Do not retry blindly. Read $LOG; rollback with bin/restore.sh if the device misbehaves." >&2
  exit 1
fi
