#!/usr/bin/env python3
"""Add extra overlay.d files to a Magisk-patched init_boot (header v4, lz4-legacy ramdisk) and write a new image.

Usage: repack-init-boot.py <in.img> <out.img> <add_dir>
  Every regular file under <add_dir> is inserted into the ramdisk cpio at its relative path
  (e.g. <add_dir>/overlay.d/f50diag.rc -> overlay.d/f50diag.rc). Existing entries are never overwritten.
  Mode: *.sh and files under sbin/ get 0755, everything else 0644.

Layout handling: the 4096-byte header is copied and only ramdisk_size is updated. The AVB vbmeta blob that
sits after the ramdisk (AVB0) and the AVB footer (AVBf, last 64 bytes) are preserved; if the new ramdisk
needs more pages, the vbmeta blob is moved and the footer's original_image_size / vbmeta_offset are rewritten.
Uses the `lz4` CLI (brew install lz4) for legacy-frame (de)compression.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys

PAGE = 4096
LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"


def lz4_decompress(data: bytes) -> bytes:
    assert data[:4] == LZ4_LEGACY_MAGIC, "ramdisk is not lz4 legacy"
    return subprocess.run(["lz4", "-d", "-c"], input=data, capture_output=True, check=True).stdout


def lz4_compress(data: bytes) -> bytes:
    out = subprocess.run(["lz4", "-l", "-9", "-c"], input=data, capture_output=True, check=True).stdout
    assert out[:4] == LZ4_LEGACY_MAGIC
    return out


def cpio_parse(d: bytes):
    off, entries = 0, []
    while off + 110 <= len(d):
        if d[off:off + 6] != b"070701":
            raise SystemExit(f"bad cpio magic at {off}")
        f = [int(d[off + 6 + 8 * i:off + 14 + 8 * i], 16) for i in range(13)]
        _ino, mode, uid, gid, _nlink, _mtime, fsize, _dmaj, _dmin, rmaj, rmin, nsize, _chk = f
        name = d[off + 110:off + 110 + nsize - 1]
        hdr = (110 + nsize + 3) & ~3
        data = d[off + hdr:off + hdr + fsize]
        off += (hdr + fsize + 3) & ~3
        if name == b"TRAILER!!!":
            break
        entries.append({"name": name, "mode": mode, "uid": uid, "gid": gid, "data": data, "rmaj": rmaj, "rmin": rmin})
    return entries


def cpio_build(entries) -> bytes:
    out = bytearray()
    ino = 300000
    for e in entries:
        ino += 1
        name = e["name"] + b"\0"
        data = e["data"]
        out += b"070701" + b"".join(f"{v:08x}".encode() for v in (
            ino, e["mode"], e["uid"], e["gid"], 1, 0, len(data), 0, 0, e["rmaj"], e["rmin"], len(name), 0)) + name
        out += b"\0" * ((-len(out)) % 4)
        out += data
        out += b"\0" * ((-len(out)) % 4)
    name = b"TRAILER!!!\0"
    out += b"070701" + b"".join(f"{v:08x}".encode() for v in (0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(name), 0)) + name
    out += b"\0" * ((-len(out)) % 4)
    out += b"\0" * ((-len(out)) % 512)
    return bytes(out)


def page_end(size: int) -> int:
    return PAGE + ((size + PAGE - 1) // PAGE) * PAGE


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    src, dst, add_dir = argv[1], argv[2], argv[3]
    img = open(src, "rb").read()
    assert img[:8] == b"ANDROID!"
    kernel_size, ramdisk_size = struct.unpack("<II", img[8:16])
    header_version = struct.unpack("<I", img[40:44])[0]
    assert kernel_size == 0 and header_version == 4, "expected init_boot header v4 with no kernel"
    header = bytearray(img[:PAGE])
    old_end = page_end(ramdisk_size)

    # AVB footer (last 64 bytes): magic, major, minor, original_image_size, vbmeta_offset, vbmeta_size
    footer = img[-64:]
    has_footer = footer[:4] == b"AVBf"
    if has_footer:
        _maj, _min, orig_size, vb_off, vb_size = struct.unpack(">IIQQQ", footer[4:36])
        assert img[vb_off:vb_off + 4] == b"AVB0", "footer does not point at a vbmeta blob"
        assert vb_off >= old_end, "vbmeta overlaps the ramdisk"
        vbmeta = img[vb_off:vb_off + vb_size]
        print(f"AVB footer: original_image_size={orig_size} vbmeta_offset={vb_off} vbmeta_size={vb_size}")

    entries = cpio_parse(lz4_decompress(img[PAGE:PAGE + ramdisk_size]))
    existing = {e["name"] for e in entries}
    added = []
    for root, _, files in os.walk(add_dir):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, add_dir).replace(os.sep, "/").encode()
            parts = rel.split(b"/")[:-1]
            for i in range(1, len(parts) + 1):
                dname = b"/".join(parts[:i])
                if dname not in existing:
                    entries.append({"name": dname, "mode": 0o040755, "uid": 0, "gid": 0, "data": b"", "rmaj": 0, "rmin": 0})
                    existing.add(dname)
            if rel in existing:
                raise SystemExit(f"refusing to overwrite existing ramdisk entry: {rel.decode()}")
            mode = 0o100755 if (fn.endswith(".sh") or "/sbin/" in "/" + rel.decode()) else 0o100644
            entries.append({"name": rel, "mode": mode, "uid": 0, "gid": 0, "data": open(full, "rb").read(), "rmaj": 0, "rmin": 0})
            existing.add(rel)
            added.append(rel.decode())

    new_rd = lz4_compress(cpio_build(entries))
    new_end = page_end(len(new_rd))
    struct.pack_into("<I", header, 12, len(new_rd))

    out = bytearray(len(img))
    out[:PAGE] = header
    out[PAGE:PAGE + len(new_rd)] = new_rd
    if has_footer:
        new_vb_off = vb_off if new_end <= vb_off else new_end
        assert new_vb_off + vb_size <= len(img) - 64, "vbmeta would overlap the footer"
        out[new_vb_off:new_vb_off + vb_size] = vbmeta
        new_footer = bytearray(footer)
        struct.pack_into(">QQQ", new_footer, 12, new_end if orig_size == old_end else orig_size, new_vb_off, vb_size)
        out[-64:] = new_footer
        print(f"vbmeta now at {new_vb_off} (moved={new_vb_off != vb_off}); footer original_image_size={struct.unpack('>Q', new_footer[12:20])[0]}")
    else:
        out[new_end:] = img[old_end:old_end + (len(img) - new_end)]

    open(dst, "wb").write(out)
    print(f"added: {added}")
    print(f"ramdisk: {ramdisk_size} -> {len(new_rd)} bytes (page end {old_end} -> {new_end}); image size {len(out)}")

    # self-check
    chk = bytes(out)
    rs = struct.unpack("<I", chk[12:16])[0]
    names = [e["name"].decode() for e in cpio_parse(lz4_decompress(chk[PAGE:PAGE + rs]))]
    for a in added:
        assert a in names, a
    assert chk[-64:-60] == b"AVBf" if has_footer else True
    print("self-check ok:", len(names), "cpio entries")


if __name__ == "__main__":
    main(sys.argv)
