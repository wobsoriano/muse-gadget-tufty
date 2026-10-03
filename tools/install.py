#!/usr/bin/env python3
"""Install the Muse app into the badge's menu.

    tools/install.py [--uninstall] [--volume /Volumes/TUFTY]

The badge's app folder is only writable in USB disk mode, where the badge
mounts on this computer as a drive. This switches a plugged-in badge into disk
mode (double-tap RESET if that fails), copies `app/muse` to the drive's
`apps/muse`, adds Muse to the menu's order file, and ejects the drive, which
restarts the badge. Running it again updates the app in place.
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "app" / "muse"
APP_NAME = "muse"
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def mounted_badges():
    return [v for v in pathlib.Path("/Volumes").iterdir() if (v / "apps" / "menu").is_dir()]


def find_volume():
    """The badge's drive, switching the badge into disk mode first if needed."""
    if not mounted_badges():
        print("asking the badge to enter disk mode...")
        try:
            # The call restarts the badge, so mpremote never gets an answer.
            subprocess.run([sys.executable, str(ROOT / "tools" / "badge.py"), "exec",
                            "import powman; powman.reset_into_msc()"],
                           capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            subprocess.run(["pkill", "-f", "mpremote connect"], check=False)
        deadline = time.monotonic() + 30
        while not mounted_badges() and time.monotonic() < deadline:
            time.sleep(1)
    candidates = mounted_badges()
    if len(candidates) != 1:
        sys.exit("expected one badge drive with an apps/menu folder, found %d. "
                 "Double-tap RESET on the badge, or pass --volume." % len(candidates))
    return candidates[0]


def set_listed(volume, listed):
    """Add Muse to, or remove it from, the menu's order file, if the menu has one.

    Some badge firmwares only show the apps this file names. Muse goes first,
    so it is the app under the cursor after a reset.
    """
    order = volume / "apps" / "menu" / "order.txt"
    if not order.exists():
        return
    names = [line for line in order.read_text().split("\n") if line and line != APP_NAME]
    if listed:
        names.insert(0, APP_NAME)
    order.write_text("\n".join(names) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--volume", help="the badge's drive (default: the one mounted)")
    parser.add_argument("--uninstall", action="store_true", help="remove the app instead")
    parser.add_argument("--no-eject", action="store_true", help="leave the drive mounted")
    args = parser.parse_args()

    volume = pathlib.Path(args.volume) if args.volume else find_volume()
    target = volume / "apps" / APP_NAME
    if target.exists():
        shutil.rmtree(target)
    if args.uninstall:
        set_listed(volume, False)
        print("removed Muse from %s" % volume)
    else:
        if not (SOURCE / "musebadge" / "avatar" / "manifest.json").exists():
            print("note: no avatar sprites in app/muse/musebadge/avatar, so the badge will "
                  "show a plain face. Run tools/avatar_sprites.py first to get the character.")
        shutil.copytree(SOURCE, target, ignore=SKIP)
        # With the avatar generated, its head replaces the plain icon.
        head = SOURCE / "musebadge" / "avatar" / "icon.png"
        if head.exists():
            shutil.copyfile(head, target / "icon.png")
        set_listed(volume, True)
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print("installed Muse to %s (%.0f KB)" % (target, size / 1024))

    if not args.no_eject:
        subprocess.run(["sync"], check=False)
        ejected = subprocess.run(["diskutil", "eject", str(volume)], capture_output=True, text=True)
        print("ejected. The badge restarts into its menu." if ejected.returncode == 0
              else "could not eject %s: eject it yourself before unplugging.\n%s"
              % (volume, ejected.stderr.strip()))


if __name__ == "__main__":
    main()
