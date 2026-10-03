#!/usr/bin/env python3
"""Send a message to your Muse through the badge and print the reply.

    tools/ask_muse.py "What does your avatar look like?"
    tools/ask_muse.py --file prompt.md [--out reply.md]

The badge holds the pairing, so this computer needs no token. It interrupts
the Muse app on the badge for the duration; relaunch it afterwards.
"""
import argparse, base64, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BADGE = [sys.executable, str(ROOT / "tools" / "badge.py")]


def ask(prompt):
    staged = ROOT / ".ask_prompt.txt"
    staged.write_text(prompt)
    try:
        port = subprocess.run(BADGE + ["port"], capture_output=True, text=True).stdout.strip()
        proc = subprocess.run(BADGE + ["send", str(staged), "/state/muse/ask_prompt.txt",
                                       str(ROOT / "tools" / "device" / "ask.py")],
                              capture_output=True, text=True)
    finally:
        staged.unlink()
    lines = proc.stdout.splitlines()
    for line in lines:
        if line.startswith(("ASK-ERROR", "ASK-NAME", "ASK-PART")):
            print(line, file=sys.stderr)
    if "ASK-END" not in lines:
        sys.exit("no reply from the badge:\n" + "\n".join(lines[-15:]) + proc.stderr[-800:])
    begin = next(i for i, l in enumerate(lines) if l.startswith("ASK-BEGIN"))
    return base64.b64decode("".join(lines[begin + 1:lines.index("ASK-END")])).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("message", nargs="?")
    parser.add_argument("--file")
    parser.add_argument("--out")
    args = parser.parse_args()
    prompt = pathlib.Path(args.file).read_text() if args.file else args.message
    if not prompt:
        parser.error("give a message or --file")
    reply = ask(prompt)
    if args.out:
        pathlib.Path(args.out).write_text(reply)
        print("saved %d characters to %s" % (len(reply), args.out))
    else:
        print(reply)


if __name__ == "__main__":
    main()
