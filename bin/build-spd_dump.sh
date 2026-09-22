#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/vendor/spreadtrum_flash"
OUT="$ROOT/build/spd_dump"
BREW="$(brew --prefix)"

if [[ ! -d "$SRC/.git" ]]; then
  git clone --depth 1 https://github.com/TomKing062/spreadtrum_flash "$SRC"
fi
[[ -f "$BREW/include/libusb-1.0/libusb.h" ]] || { echo "libusb headers missing: brew install libusb" >&2; exit 1; }

mkdir -p "$ROOT/build"
printf '#define GIT_VER "%s"\n#define GIT_SHA1 "%s"\n' \
  "$(git -C "$SRC" rev-parse --abbrev-ref HEAD)" "$(git -C "$SRC" rev-parse HEAD)" > "$SRC/GITVER.h"

cc -O2 -Wall -std=c99 -DUSE_LIBUSB=1 -I"$BREW/include" \
  -o "$OUT" "$SRC/spd_dump.c" "$SRC/common.c" \
  -L"$BREW/lib" -lusb-1.0 -lm -lpthread

echo "built $OUT"
file "$OUT"
"$OUT" --help 2>&1 | grep -q -- '--kickto' && echo "ok: --kickto supported"
