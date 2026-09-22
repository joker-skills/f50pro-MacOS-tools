#!/usr/bin/env bash
# Read-only: dump one or more partitions from download mode into OUT_DIR (new dir), then reset the device.
# Usage:  OUT_DIR=~/f50pro-backups/read-<tag> bin/read-part.sh sd_klog [more partitions...]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPD_DUMP="${SPD_DUMP:-$ROOT/build/spd_dump}"
KICK="${KICK:-2}"
WAIT="${WAIT:-300}"
: "${OUT_DIR:?set OUT_DIR to a NEW directory}"
[[ $# -ge 1 ]] || { echo "usage: OUT_DIR=... $0 <partition> [partition...]" >&2; exit 1; }
die() { echo "error: $*" >&2; exit 1; }

[[ -x "$SPD_DUMP" ]] || die "spd_dump not built"
[[ "$(id -u)" -ne 0 ]] || die "do not run as root"
[[ ! -e "$OUT_DIR" ]] || die "OUT_DIR already exists: $OUT_DIR"
mkdir -p "$OUT_DIR"; OUT_DIR="$(cd "$OUT_DIR" && pwd)"

state="$(python3 "$ROOT/bin/usb-state.py" --absent-for 6 || true)"
[[ "$state" == "ABSENT" ]] || die "F50 Pro was seen on the bus within the last 6 s ($state). Unplug it first."

args=()
for p in "$@"; do args+=(r "$p"); done
echo "reading: $* -> $OUT_DIR"
echo ">>> Now plug the F50 Pro directly into the Mac. <<<"
"$SPD_DUMP" --wait "$WAIT" --kickto "$KICK" --verbose 0 path "$OUT_DIR" "${args[@]}" reset 2>&1 | tee "$OUT_DIR/spd_dump.log" | grep -vE '^\[=*'
/bin/ls -l "$OUT_DIR" | awk 'NR>1{printf "%12s  %s\n",$5,$9}'
