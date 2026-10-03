#!/usr/bin/env python3
"""Drive the Tufty badge over USB with mpremote.

  tools/badge.py push            copy changed package files to the badge's development copy
  tools/badge.py run SCRIPT      run a local script on the badge
  tools/badge.py exec CODE       run a line of code on the badge
  tools/badge.py send FILE DEST SCRIPT   copy FILE to DEST on the badge, then run SCRIPT
  tools/badge.py token           store the SDK token from .sdk_token on the badge
  tools/badge.py reset           reboot the badge into its normal menu

The badge firmware arms an 8 second hardware watchdog that keeps running after
mpremote interrupts the main loop, so every session starts by installing a
timer that feeds it. Without that, any session longer than 8 seconds ends in
"Device not configured".
"""

import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / ".push_cache.json"
PORT_HINT = "Tufty 2350"

# Local directory -> directory on the badge. This is the development copy:
# /lib is writable over USB, while the menu app in /system/apps is only
# writable in disk mode (tools/install.py).
TARGETS = (
    (ROOT / "app" / "muse" / "musebadge", "/lib/musebadge"),
)

FEED = (
    "import machine\n"
    "_wdt = machine.WDT(timeout=8000)\n"
    "_wdt_timer = machine.Timer(period=1000, callback=lambda t: _wdt.feed())\n"
)


def port():
    listing = subprocess.run(["mpremote", "connect", "list"], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        if PORT_HINT in line:
            return line.split()[0]
    sys.exit("badge not found: is it plugged in with a data cable?")


def mpremote(*commands, timeout=600):
    args = ["mpremote", "connect", port(), "exec", FEED]
    for command in commands:
        args += ["+", *command]
    return subprocess.run(args, timeout=timeout).returncode


def push():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    commands, dirs, sent = [], set(), {}
    for local, remote in TARGETS:
        if not local.exists():
            continue
        for path in sorted(local.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.name.startswith("."):
                continue
            dest = remote + "/" + path.relative_to(local).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if cache.get(dest) == digest:
                continue
            parent = dest.rsplit("/", 1)[0]
            parts = parent.strip("/").split("/")
            for i in range(1, len(parts) + 1):
                dirs.add("/" + "/".join(parts[:i]))
            commands.append(["cp", str(path), ":" + dest])
            sent[dest] = digest
    if not commands:
        print("badge is up to date")
        return 0
    mkdirs = "import os\nfor d in %r:\n try: os.mkdir(d)\n except OSError: pass\n" % (sorted(dirs),)
    code = mpremote(["exec", mkdirs], *commands)
    if code == 0:
        cache.update(sent)
        CACHE.write_text(json.dumps(cache, indent=1))
        print("pushed %d files" % len(sent))
    return code


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "push":
        return push()
    if command == "run":
        # No timeout: the app runs until interrupted and this follows its log.
        return mpremote(["run", rest[0]], timeout=None)
    if command == "send":
        # Copy one file to the badge, then run a script, in a single session.
        return mpremote(["cp", rest[0], ":" + rest[1]], ["run", rest[2]], timeout=None)
    if command == "port":
        print(port())
        return 0
    if command == "exec":
        return mpremote(["exec", rest[0]])
    if command == "token":
        token = (ROOT / ".sdk_token").read_text().strip()
        code = (
            "import os, json\n"
            "for d in ('/state', '/state/muse'):\n"
            " try: os.mkdir(d)\n"
            " except OSError: pass\n"
            "open('/state/muse/sdk_token.json', 'w').write(json.dumps({'token': %r}))\n"
            "print('SDK token stored')\n" % token
        )
        return mpremote(["exec", code])
    if command == "reset":
        return subprocess.run(["mpremote", "connect", port(), "reset"]).returncode
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
