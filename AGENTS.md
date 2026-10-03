# AGENTS.md

How to work on this port, test it, and get it onto a badge. See `README.md`
for the shorter human overview.

## What this is

A MicroPython app that makes a Pimoroni Badgeware Tufty 2350 into a Muse
gadget. It is a port of the Linux client in Meta's Muse Gadget SDK
(`vendor/muse-gadget-sdk/linux`, cloned separately and ignored by git) and
speaks the same pairing and control protocols, so the same Muse app pairs it.

`app/muse` is the whole app, and the folder that gets copied to the badge.

| File in `app/muse/musebadge` | Role | Ported from |
|---|---|---|
| `main.py` | Wires screen, Wi-Fi, BLE setup and the session together. Commands and button prompts | new |
| `ui.py` | The screen: avatar sprite player, message view, button polling | new |
| `service.py` | Reconnect loop, token rotation, connection state | `service.py` |
| `link.py` | One session: Noise handshake, `/link-control`, `/chat/stream`, `/chat/subscribe` | `link_client.py` |
| `noise.py` | Noise XX initiator, frame chunking, service envelopes | `noise/` |
| `pairing.py` | Community pairing v5 | `pairing.py` |
| `setup.py` | BLE setup commands, as one asyncio consumer | `ble_setup.py` |
| `ble.py` | GATT peripheral on `aioble` | `ble_server.py` (BlueZ) |
| `api.py` | `fetch_vms` and device token refresh | `muse_api.py` |
| `store.py` | JSON state files and the device identity | `config.py`, `identity.py` |
| `net.py` | Async HTTP and WebSocket clients over asyncio streams | new |
| `mcrypto.py` | X25519, P-256 ECDH, HKDF, HMAC, AES-GCM in pure Python | new |
| `wifi.py` | Join, scan, rejoin with backoff, set the clock | new |
| `proto.py`, `ble_framing.py` | Protobuf wire helpers and BLE chunking | copied, imports trimmed |

`ble.py`, `wifi.py`, `ui.py` and `main.py` only import on the badge. Everything
else also runs under CPython, which is how the tests exercise it.

## Tests

No badge or network needed.

```sh
uv run --with pytest --with cryptography --with websockets pytest tests/
micropython tests/kat_mcrypto.py
```

- `test_mcrypto.py` checks every primitive against the `cryptography` package
  and RFC vectors. `kat_mcrypto.py` is the same vectors without pytest, for
  the MicroPython unix port (`brew install micropython`).
- `test_interop.py` runs the port against the SDK's own client: its Noise
  responder, its envelope codec, and its `PairingSession` fed the published
  vectors.
- `test_setup.py` drives the BLE setup conversation with a phone written from
  the protocol.
- `test_session.py` starts `fake_muse.py` and runs `device_session.py` against
  it over real sockets, under CPython and MicroPython.

`fake_muse.py` mirrors event shapes captured from the real service. Change it
only to match something you observed, never to make a test pass.

## Working with the badge

```sh
tools/badge.py push                              # copy changed files to /lib/musebadge
tools/badge.py run tools/device/run_app.py       # run the app and follow its log
tools/badge.py run tools/device/run_installed.py # run the installed menu app instead
tools/screenshot.py /tmp/shots                   # save every screen as a PNG
uv run --with bleak --with cryptography tools/phone_sim.py   # pair from this computer
```

`push` writes a development copy to `/lib/musebadge`. The menu app in
`/system/apps/muse` has its own copy and never reads the development one, so
run `tools/install.py` in disk mode to update what the menu launches.

Things that cost time to find:

- **The Supabase Select firmware arms an 8 second hardware watchdog** that
  keeps running after `mpremote` interrupts the main loop. Any session over 8
  seconds ends in "Device not configured". `tools/badge.py` installs a timer
  that feeds it before every command. Stock Pimoroni firmware has no watchdog.
- **`/system` is read-only to code.** Only disk mode (double-tap RESET) can
  write it. `/` is a 1 MB writable LittleFS, which holds `/lib` and `/state`.
- **`mpremote` hangs while the badge is in disk mode.** Eject the drive and
  press RESET first.
- **`mpremote cp -r` on `/system` fails.** Copy file by file.
- **Attaching over USB interrupts the running app.** The app also logs to
  `/state/muse/log.txt` so you can read what happened afterwards.
- **USB dropping is usually the cable.** The app keeps running on battery.
  Check `machine.reset_cause()` and the on-badge log before assuming a crash.
- **A captive portal joins and then times out** on every HTTPS request, which
  the Muse app reports as "Couldn't connect". `tools/device/check_net.py`
  probes for one.
- **The Supabase menu only lists apps named in `apps/menu/order.txt`.**
  `tools/install.py` adds the line.

State lives in `/state/muse`: `identity.json`, `pairing.json`, `wifi.json`,
`sdk_token.json` and `log.txt`.

## MicroPython limits that shaped the code

- `cryptolib` has AES in ECB and CBC only. No CTR, no GCM.
- No `hmac`, `dataclasses`, `typing`, `secrets`, `threading`, `__future__`.
- No `int.bit_length()`, no `re.fullmatch`, no `asyncio.Semaphore`, no
  `asyncio.Queue`, no `asyncio.wait`.
- `requests` blocks the event loop and skips certificate checks, hence
  `net.py`.
- Annotations are parsed and discarded, so a file may keep them, but CPython
  evaluates them. `proto.py` defines `Tuple = tuple` for that reason.

On the badge one X25519 operation takes about 230 ms, one P-256 operation
320 ms, and AES-GCM about 48 ms per KB. The Noise handshake keys and the
pairing key are generated before the peer is waiting.

## Talking to Muse

Everything in the SDK's `linux/AGENTS.md` under "Talking to the Muse" holds.
On top of it, found by probing the live service with a homehub device token:

- `/identity` and `/chat/history` answer 403, "path not allowed for device
  token". Do not add them back.
- `/chat/subscribe` is allowed. It is a stream that never ends, of
  newline-delimited JSON events, opened once per session.
- Reply events do not link to the question. `delta.message_start` has an empty
  `reply_to_message_id`, `delta.text_append` names its own message as
  `parent_message_id`, and `delta.message_done` carries no text. `_Turn` in
  `link.py` treats a message that starts after our `message.user` echo as the
  reply.
- Nothing marks the end of a turn. It is over when every reply message is done
  and the stream has been quiet for 3 seconds.
- Muse reports the gadget as offline if it invokes a command while the app is
  not running.
- Both hosts chain to DigiCert Global Root G2, bundled as
  `digicert_global_root_g2.der`. TLS is verified against that root only.
- Keep `platform: "linux"`, `device_family: "homehub"`. Never use family
  `link` or advertise `device.ota`.

## Adding a command

1. Add a spec to `COMMANDS` in `main.py`. The description is what Muse reads
   to decide when to call it, so say when to use it.
2. Handle it in `App.run_command`. Return `{"ok": True, "payload": {...}}` or
   `{"ok": False, "error": "message"}`.
3. Muse sees it after the app restarts and registers again.

## The avatar

The SDK draws its character with a C renderer. `tools/avatar_sprites.py`
builds it with the SDK's host driver, plus `tools/avatar_dance.c` for the
dance, and writes one sprite sheet per animation with a manifest. `ui.Avatar`
plays them. The output folder is ignored by git because the default character
is Meta's and not Apache licensed. Without sprites the app draws a plain face.

## Rules

- Never print, log or commit the SDK token. It lives in `.sdk_token` and on
  the badge.
- `backup/` holds a copy of a badge's own files, including its credentials.
  It is ignored. Keep it that way.
- Say Muse, never Hatch, in anything a person reads. `hatch` stays only in
  wire identifiers the server or app depends on, such as `hatch_link`,
  `hatch-link:` and `hatch_refresh:`.
- Files with a Meta copyright header are ports. Keep the header and its
  modification note.

## Before you hand back work

1. The host tests pass, and `micropython tests/kat_mcrypto.py` prints `PASS`
   if you touched `mcrypto.py`.
2. If you changed the screen, `tools/screenshot.py` output looks right.
3. If you deployed, the log reaches `registered with the Muse` with no
   traceback.
