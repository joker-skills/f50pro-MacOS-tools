#!/usr/bin/env python3
"""Classify the ZTE F50 Pro USB state on macOS from ioreg. Prints one token per line.

Tokens:
  DOWNLOAD     1782:4d00  Spreadtrum/Unisoc boot-ROM download mode (what spd_dump needs)
  GADGET_NCM   19d2:1354  normal composite (NCM network + CDC data + MTP)
  GADGET_RNDIS 19d2:0246  composite when WebUI USB protocol = RNDIS
  GADGET_2D00  19d2:2d00  transitional single vendor-interface composition seen in the 3 s loop
  OTHER        any other ZTE (19d2) or Spreadtrum (1782) device
  ABSENT       nothing from those vendors

Options:
  --watch [SECONDS]   keep sampling every second (default 60 s) and print state changes with timestamps.
  --absent-for N      sample every second for N seconds; print ABSENT and exit 0 only if every sample was
                      ABSENT, otherwise print the first non-ABSENT state and exit 1. Use this as the
                      "device is unplugged" interlock: the F50 Pro's 3 s re-enumeration loop has ~1 s gaps
                      in which a single sample would wrongly read ABSENT.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time

VID_ZTE = 0x19D2
VID_SPRD = 0x1782
NAMES = {
    (VID_SPRD, 0x4D00): "DOWNLOAD",
    (VID_ZTE, 0x1354): "GADGET_NCM",
    (VID_ZTE, 0x0246): "GADGET_RNDIS",
    (VID_ZTE, 0x2D00): "GADGET_2D00",
}
DEVICE_RE = re.compile(r"\+-o .*<class (?:IOUSBHostDevice|IOUSBDevice)(?:[, >])")
FIELD_RE = re.compile(r'"(idVendor|idProduct)"\s*=\s*(0[xX][0-9a-fA-F]+|[0-9]+)')


def ioreg() -> str:
    return subprocess.check_output(["ioreg", "-p", "IOUSB", "-l", "-w", "0"], text=True, errors="replace")


def classify(text: str) -> str:
    devices: list[dict[str, int]] = []
    current: dict[str, int] | None = None
    for line in text.splitlines():
        if DEVICE_RE.search(line):
            if current is not None:
                devices.append(current)
            current = {}
            continue
        if current is None:
            continue
        m = FIELD_RE.search(line)
        if m:
            current[m.group(1)] = int(m.group(2), 0)
    if current is not None:
        devices.append(current)
    pairs = {(d.get("idVendor"), d.get("idProduct")) for d in devices}
    for key, name in NAMES.items():
        if key in pairs:
            return name
    if any(v in (VID_ZTE, VID_SPRD) for v, _ in pairs):
        return "OTHER"
    return "ABSENT"


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--absent-for":
        seconds = int(argv[1]) if len(argv) > 1 else 5
        for _ in range(seconds):
            state = classify(ioreg())
            if state != "ABSENT":
                print(state)
                return 1
            time.sleep(1)
        print("ABSENT")
        return 0
    if argv and argv[0] == "--watch":
        seconds = int(argv[1]) if len(argv) > 1 else 60
        last = None
        end = time.time() + seconds
        while time.time() < end:
            state = classify(ioreg())
            if state != last:
                print(time.strftime("%H:%M:%S"), state, flush=True)
                last = state
            time.sleep(1)
        return 0
    print(classify(ioreg()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ioreg failed: {exc}", file=sys.stderr)
        sys.exit(2)
