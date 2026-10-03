# Muse on a Tufty badge

A Muse gadget that runs on the Pimoroni Badgeware Tufty 2350, the conference
badge with a 320x240 colour screen. It is a MicroPython port of the Linux
client from Meta's [Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk).
Pair the badge in the Muse app and Muse can put messages on its screen, make
its avatar dance, and set its lights. Press a button and the badge asks Muse
something and shows the answer.

> **Note:** This is a community project. It is not made or endorsed by Meta.
> It talks to Muse the same way the SDK's Linux client does, and it could stop
> working if that service changes. Proceed at your own risk.

## What you need

- **A Tufty 2350 badge** running Badgeware MicroPython 1.29 or later. It was
  built and tested on the Supabase Select 2026 badge. A stock Pimoroni Tufty
  2350 should work but is untested.
- **A USB-C cable that carries data**, and a computer running macOS or Linux.
- **An SDK token** from [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens).
  Read the [Gadget SDK Terms](https://gadgets.muse.ai/sdk-terms) first. They
  say not to publish your token, so keep `.sdk_token` out of git. This
  repository already ignores it.
- **The Muse app** on your phone, to pair the badge.
- **Wi-Fi without a sign-in page.** The badge has no browser, so hotel and
  conference networks with a captive portal join and then block everything.
  A phone hotspot works. The radio is 2.4 GHz only.
- [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
  and [`uv`](https://docs.astral.sh/uv/) on the computer.

## Install

```sh
git clone https://github.com/wobsoriano/muse-gadget-tufty.git
cd muse-gadget-tufty
git clone --depth 1 https://github.com/facebookincubator/muse-gadget-sdk vendor/muse-gadget-sdk
```

The SDK checkout is used to draw the avatar and to run the tests. It is not
copied to the badge.

### 1. Draw the avatar

```sh
uv run --with pillow tools/avatar_sprites.py
```

This builds the SDK's avatar renderer on your computer and packs its
animations into `app/muse/musebadge/avatar/`. Add `--supabase-logo` to put a
Supabase bolt on the character's chest. Skip this step and the badge shows a
plain drawn face.

The default character is Meta's and is not covered by the Apache licence, so
the generated sprites are ignored by git. To draw your own Muse's avatar, see
[Your own avatar](#your-own-avatar).

### 2. Store your SDK token

Plug the badge in, then run this.

```sh
echo 'mgst_…' > .sdk_token
tools/badge.py token
```

### 3. Copy the app to the badge

The badge's app folder is only writable in USB disk mode, where the badge
shows up on your computer as a drive. With the badge plugged in, run:

```sh
tools/install.py
```

It switches the badge into disk mode, copies `app/muse` to the drive, adds
Muse to the menu, and ejects the drive. The badge restarts, and **Muse** is
the first app in its menu. If the drive does not appear, double-tap **RESET**
on the back of the badge and run it again.

You can also drag `app/muse` into the drive's `apps` folder yourself. On
badges whose menu has an `apps/menu/order.txt`, add a line saying `muse` to it.

### 4. Pair it with Muse

Open Muse from the badge menu. The screen says "Not set up" and shows a name
like `MuseGadget0A1B2C`. In the Muse app:

1. Turn on **Settings > Devices > Developer mode**.
2. Tap **Add Device** and pick that name.
3. Accept the community device warning.
4. Pick your Wi-Fi network and enter its password.

The screen says "Connected" when Muse is reachable. Pairing and Wi-Fi are
saved on the badge, so it reconnects by itself after a restart.

Pairing has no manufacturer verification and cannot stop an active
man-in-the-middle attack, as the SDK notes for all community devices. Set it
up somewhere you trust.

## Use it

Ask Muse things like these.

> Show "Hello Supabase Select" on my badge.

> Make my badge dance.

> Set my badge lights to half.

Muse decides which command a sentence maps to. Naming the badge helps. "Make
Muse dance" on its own tends to produce a picture in the app.

| Command | What it does |
|---|---|
| `badge.show_message` | Shows a short text message, with an optional title |
| `badge.clear` | Clears the message and goes back to the avatar |
| `badge.dance` | Makes the avatar dance for a few seconds |
| `badge.set_lights` | Sets the brightness of the four LEDs on the back |
| `device.health` | Reports uptime, free memory, battery, Wi-Fi and version |

| Button | What it does |
|---|---|
| **A** | Asks Muse for a one line joke and shows the answer |
| **B** | Clears the message |
| **C** | Asks Muse for a line of encouragement |
| **UP** | Pets the avatar |
| **DOWN** | Makes the avatar dance |
| **HOME** | Leaves the app and returns to the badge menu |

The two prompts live in `BUTTON_PROMPTS` in `app/muse/musebadge/main.py`.

The badge runs on its battery, so it keeps working unplugged. Muse is only
connected while the Muse app is open on the badge. Opening another badge app
disconnects it until you come back.

## Your own avatar

The SDK's recipe asks your Muse to redraw its own avatar as a pixel-art
renderer. `tools/ask_muse.py` sends that request through the badge, so your
computer needs no token.

```sh
cat vendor/muse-gadget-sdk/esp32/tools/muse/avatar_prompt.md \
    vendor/muse-gadget-sdk/esp32/avatar/muse_pixel.c > prompt.md
tools/ask_muse.py --file prompt.md --out reply.md
```

Save the C file from the reply as `my_avatar.c`, then render and reinstall.

```sh
uv run --with pillow tools/avatar_sprites.py --src my_avatar.c
```

This path is untested end to end. See the SDK's
[`AVATAR_RECIPE.md`](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/esp32/tools/muse/AVATAR_RECIPE.md)
for what a good reply looks like.

## How it works

The badge cannot run the SDK's firmware, which is built for ESP32 chips, and
MicroPython lacks most of what the Linux client imports. So the port
reimplements the client on what the badge has.

- **Pairing over Bluetooth** with the Muse app, using P-256 key agreement and
  AES-GCM records.
- **An encrypted Noise session** to your Muse over a WebSocket, carrying the
  control stream and chat requests.
- **Its own crypto.** MicroPython ships only AES in ECB mode and SHA-256, so
  `mcrypto.py` implements X25519, P-256, HKDF and AES-GCM in Python.
- **The avatar as sprite sheets.** The SDK's renderer is too heavy to run per
  frame in MicroPython, so it runs on your computer at build time.

The badge registers with Muse as `platform: linux`, `device_family: homehub`,
the values the SDK's Linux client uses. Muse therefore treats it as a Linux
gadget. It never registers as family `link`, because the server pushes ESP32
firmware updates to those.

## Develop

Run the tests on your computer. They need no badge.

```sh
uv run --with pytest --with cryptography --with websockets pytest tests/
```

They check the port against the SDK's own Python client. That covers the
Noise handshake and envelopes, the pairing vectors, and a whole session over
real sockets against a local stand-in for Muse. The session test also runs under the
MicroPython unix port if `micropython` is installed.

To iterate on a badge without disk mode, push a development copy and run it.

```sh
tools/badge.py push
tools/badge.py run tools/device/run_app.py
```

| Tool | What it does |
|---|---|
| `tools/install.py` | Installs or removes the menu app, in disk mode |
| `tools/badge.py` | Pushes the development copy, runs scripts, stores the token |
| `tools/avatar_sprites.py` | Renders the avatar animations into sprite sheets |
| `tools/screenshot.py` | Saves each screen of the app from the badge's framebuffer |
| `tools/phone_sim.py` | Plays the Muse app's side of pairing from your computer |
| `tools/ask_muse.py` | Sends a message to Muse through the badge and prints the reply |

[`AGENTS.md`](AGENTS.md) has the details a coding agent needs, including the
badge quirks that cost the most time.

## Known limits

- No microphone or speaker, so there is no voice.
- No pictures yet. The SDK's `display.draw_url` command is not implemented.
- A lost connection is noticed after about 45 seconds, then the badge
  reconnects.
- The Supabase Select firmware arms a hardware watchdog. The app keeps it fed
  with a timer, so the watchdog no longer catches a hang inside the app.

## License

Apache 2.0. See [`LICENSE`](LICENSE). The files in `app/muse/musebadge` that
carry a Meta copyright header are ports of the Muse Gadget SDK, which is also
Apache 2.0. The SDK's default avatar is not covered by that licence and is not
included here.
