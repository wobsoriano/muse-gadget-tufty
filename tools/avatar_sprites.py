#!/usr/bin/env python3
"""Turn a Muse pixel avatar renderer into sprite sheets for the badge.

    uv run --with pillow tools/avatar_sprites.py [--src muse_pixel.c]

The Muse gadget firmware draws its avatar with a procedural C renderer
(`muse_pixel.c`), far too heavy to run per frame in MicroPython. This builds
that renderer on the host with the SDK's own animation driver, plays every
animation, and packs the 64x64 frames into one PNG sheet per animation plus a
manifest the badge reads.

Without --src it renders the SDK's default avatar. That character is Meta's
and is not covered by the SDK's Apache licence, so the output directory is
gitignored: fine on your own badge, not something to publish.
"""

import argparse
import json
import math
import pathlib
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
ESP32 = ROOT / "vendor" / "muse-gadget-sdk" / "esp32"
OUT = ROOT / "app" / "muse" / "musebadge" / "avatar"

CELL = 64
DRIVER_FPS = 25
FRAME_STEP = 2      # keep every second frame: 12.5 fps is plenty at this size
COLUMNS = 10

# name -> does it loop. The badge has no microphone, so "listening" is left out.
ANIMATIONS = {"boot": False, "idle": True, "thinking": True, "speaking": True,
              "happy": False, "error": True, "off": False, "dance": True}
DANCE_SWAY_PX = 4
OUTLINE = (0x3A, 0x2B, 0x22)    # the renderer's C_OUT colour
ICON_PX = 24                    # the badge menu draws icons 24 px tall
ICON_FRAME = 10                 # an idle frame with the eyes open
DANCE_SWAY_FRAMES = DRIVER_FPS      # one full sway a second: two beats at 120 BPM


# The Supabase bolt as the renderer's stamp bitmap: '#' is the solid left
# piece, 'o' the darker right piece.
SUPABASE_LOGO = (
    "...#...",
    "..##...",
    "..##...",
    ".###ooo",
    ".###ooo",
    "####oo.",
    "....oo.",
    "....o..",
    "....o..",
)


def with_chest_logo(source):
    """Patch a muse_pixel.c so the avatar wears the logo on its chest.

    The stamp goes in right after the body is drawn and is placed from the
    body's own pose, so it bobs, squashes and hops with the character.
    """
    def once(text, old, new):
        if text.count(old) != 1:
            sys.exit("cannot add the chest logo: this renderer has no single %r" % old)
        return text.replace(old, new)

    rows = ", ".join('"%s"' % row for row in SUPABASE_LOGO)
    source = once(source, "    C_COUNT,\n", "    C_LOGO,\n    C_LOGOD,\n    C_COUNT,\n")
    source = once(source, "    [C_WHITE] = 0xffffff,\n",
                  "    [C_WHITE] = 0xffffff,\n    [C_LOGO] = 0x3ecf8e,\n    [C_LOGOD] = 0x249361,\n")
    return once(source, "    draw_avatar(&j, arms, feet);\n", (
        "    draw_avatar(&j, arms, feet);\n"
        "    {\n"
        "        static const char *const LOGO[] = { %s };\n"
        "        stamp(LOGO, %d, iround(j.cx) - 3, iround(j.fy + j.fb) + 3, C_LOGO, C_LOGOD);\n"
        "    }\n") % (rows, len(SUPABASE_LOGO)))


def render_frames(src, frames_dir):
    exe = frames_dir.parent / "muse_anim"
    subprocess.run(
        ["cc", "-O2", "-Wall", "-I", "components/muse", "tools/muse/anim.c", str(src), "-lm",
         "-o", str(exe)],
        cwd=ESP32, check=True, capture_output=True, text=True)
    frames_dir.mkdir()
    subprocess.run([str(exe), str(frames_dir)], check=True)
    # The SDK's driver has no dance, so ours renders one against the same source.
    dance = frames_dir.parent / "muse_dance"
    subprocess.run(
        ["cc", "-O2", "-Wall", "-I", "components/muse", str(ROOT / "tools" / "avatar_dance.c"),
         str(src), "-lm", "-o", str(dance)],
        cwd=ESP32, check=True, capture_output=True, text=True)
    (frames_dir / "dance").mkdir()
    subprocess.run([str(dance), str(frames_dir / "dance")], check=True)


def cell_frame(path):
    # The driver dims the last pixel of each cell to draw a pixel grid.
    # Nearest-neighbour sampling reads the middle pixel, which is the cell's
    # true colour.
    return Image.open(path).convert("RGB").resize((CELL, CELL), Image.Resampling.NEAREST)


def head_icon(frame):
    """The character's head from one frame, cut out of its background, as a menu icon.

    The renderer draws a closed dark outline around the silhouette. Flooding
    in from the corners over everything that is not outline leaves exactly
    the character, without the aura and sparkles behind it.
    """
    w, h = frame.size
    px = frame.load()

    def is_outline(x, y):
        r, g, b = px[x, y]
        return abs(r - OUTLINE[0]) < 14 and abs(g - OUTLINE[1]) < 14 and abs(b - OUTLINE[2]) < 14

    outside = set()
    stack = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    while stack:
        x, y = stack.pop()
        if (x, y) in outside or not (0 <= x < w and 0 <= y < h) or is_outline(x, y):
            continue
        outside.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]

    cut = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    out = cut.load()
    inside = [(x, y) for y in range(h) for x in range(w) if (x, y) not in outside]
    for x, y in inside:
        out[x, y] = px[x, y] + (255,)
    left, right = min(x for x, _ in inside), max(x for x, _ in inside) + 1
    top = min(y for _, y in inside)
    side = right - left
    return cut.crop((left, top, right, top + side)).resize((ICON_PX, ICON_PX), Image.Resampling.LANCZOS)


def sway(frame, driver_frame):
    """Slide the whole figure side to side on the beat."""
    shift = round(DANCE_SWAY_PX * math.sin(2 * math.pi * driver_frame / DANCE_SWAY_FRAMES))
    moved = Image.new("RGB", frame.size)
    moved.paste(frame, (shift, 0))
    return moved


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--src", default=str(ESP32 / "avatar" / "muse_pixel.c"),
                        help="the muse_pixel.c to render (default: the SDK's default avatar)")
    parser.add_argument("--supabase-logo", action="store_true",
                        help="put the Supabase bolt on the avatar's chest")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"cell": CELL, "fps": DRIVER_FPS / FRAME_STEP, "animations": {}}
    with tempfile.TemporaryDirectory() as tmp:
        frames_dir = pathlib.Path(tmp) / "frames"
        src = pathlib.Path(args.src).resolve()
        if args.supabase_logo:
            patched = pathlib.Path(tmp) / "muse_pixel.c"
            patched.write_text(with_chest_logo(src.read_text()))
            src = patched
        try:
            render_frames(src, frames_dir)
        except subprocess.CalledProcessError as error:
            sys.exit((error.stdout or "") + (error.stderr or "") + "\nthe renderer does not build")
        for name, loops in ANIMATIONS.items():
            paths = sorted((frames_dir / name).glob("*.ppm"))[::FRAME_STEP]
            frames = [cell_frame(p) for p in paths]
            if name == "idle":
                head_icon(frames[ICON_FRAME]).save(OUT / "icon.png")
            if name == "dance":
                frames = [sway(frame, index * FRAME_STEP) for index, frame in enumerate(frames)]
            rows = (len(frames) + COLUMNS - 1) // COLUMNS
            sheet = Image.new("RGB", (COLUMNS * CELL, rows * CELL))
            for index, frame in enumerate(frames):
                sheet.paste(frame, ((index % COLUMNS) * CELL, (index // COLUMNS) * CELL))
            # One palette per sheet keeps colours from shimmering between frames.
            sheet = sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                                   dither=Image.Dither.NONE)
            target = OUT / (name + ".png")
            sheet.save(target, optimize=True)
            manifest["animations"][name] = {
                "frames": len(frames), "columns": COLUMNS, "loop": loops}
            print("%-9s %3d frames  %6.1f KB" % (name, len(frames), target.stat().st_size / 1024))
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    total = sum(p.stat().st_size for p in OUT.iterdir())
    print("total %.1f KB in %s" % (total / 1024, OUT.relative_to(ROOT)))


if __name__ == "__main__":
    main()
