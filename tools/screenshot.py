#!/usr/bin/env python3
"""Render the badge UI states on the badge and save each framebuffer as a PNG.

    tools/screenshot.py OUT_DIR
"""
import base64, pathlib, struct, subprocess, sys, zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
out_dir = pathlib.Path(sys.argv[1]); out_dir.mkdir(parents=True, exist_ok=True)
proc = subprocess.run([sys.executable, str(ROOT / "tools" / "badge.py"), "run",
                       str(ROOT / "tools" / "device" / "screens.py")], capture_output=True, text=True)
lines = proc.stdout.splitlines()
i = 0
count = 0
while i < len(lines):
    if not lines[i].startswith("SHOT "):
        if lines[i].strip():
            print(lines[i])
        i += 1
        continue
    _, name, w, h, n = lines[i].split()
    w, h = int(w), int(h)
    i += 1
    data = bytearray()
    while lines[i] != "END":
        data += base64.b64decode(lines[i]); i += 1
    i += 1
    bpp = len(data) // (w * h)
    rows = b"".join(b"\x00" + bytes(data[y * w * bpp:(y + 1) * w * bpp]) for y in range(h))
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if bpp == 4 else 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    (out_dir / (name + ".png")).write_bytes(png)
    print("saved", out_dir / (name + ".png"), w, h, bpp)
    count += 1
if proc.stderr.strip():
    print(proc.stderr[-1500:])
sys.exit(0 if count else 1)
