"""The badge screen: the Muse avatar, and the message Muse last sent.

Drawn with the Badgeware builtins (`screen`, `badge`, `display`, `color`,
`shape`, `rect`), which the firmware injects. MicroPython only.
"""

import asyncio
import json
import math
import time

import badgeware  # noqa: F401  (importing it installs the drawing builtins)

from . import service

FRAME_MS = 40
SPEAKING_MS = 3000
AVATAR_DIR = __file__.rsplit("/", 1)[0] + "/avatar"

_STATE_TEXT = {
    service.UNPAIRED: "Not set up",
    service.OFFLINE: "No Wi-Fi",
    service.CONNECTING: "Connecting",
    service.CONNECTED: "Connected",
}
# The same colours the Muse reference firmware uses for its status light.
_STATE_COLOR = {
    service.UNPAIRED: (255, 150, 40),
    service.OFFLINE: (240, 200, 40),
    service.CONNECTING: (70, 130, 255),
    service.CONNECTED: (60, 210, 120),
}
# The looping animation the avatar rests in for each state.
_STATE_ANIMATION = {
    service.UNPAIRED: "idle",
    service.OFFLINE: "error",
    service.CONNECTING: "thinking",
    service.CONNECTED: "idle",
}
# The avatar renderer's accent colour for each animation.
_ACCENT = {
    "boot": (0xA9, 0xC0, 0xFF), "idle": (0xA7, 0x7D, 0xFF), "thinking": (0xE0, 0x7B, 0xFF),
    "speaking": (0x6F, 0xF0, 0xBF), "happy": (0xA7, 0x7D, 0xFF), "error": (0xFF, 0x5C, 0x5C),
    "dance": (0x6F, 0xF0, 0xBF), "off": (0x7C, 0x72, 0xD0),
}


class Avatar:
    """Plays the sprite sheets `tools/avatar_sprites.py` makes.

    `rest` is the looping animation to fall back to. `react` plays a one-shot
    (boot, happy) or a timed burst of a loop over it.
    """

    def __init__(self, directory):
        with open(directory + "/manifest.json") as f:
            manifest = json.loads(f.read())
        self._dir = directory
        self._cell = manifest["cell"]
        self._frame_ms = 1000 / manifest["fps"]
        self._animations = manifest["animations"]
        self._sheets = {}
        self.rest = "idle"
        self._reaction = None
        self._reaction_until = None
        self._started = time.ticks_ms()
        self._playing = None

    def react(self, name, ms=None):
        if name in self._animations:
            self._reaction = name
            self._reaction_until = time.ticks_add(time.ticks_ms(), ms) if ms else None
            self._playing = None

    def current(self, now):
        """(animation name, frame index) to show at `now`."""
        name = self._reaction or self.rest
        if name != self._playing:
            self._playing = name
            self._started = now
        info = self._animations[name]
        index = int(time.ticks_diff(now, self._started) / self._frame_ms)
        if self._reaction:
            timed_out = self._reaction_until is not None and time.ticks_diff(now, self._reaction_until) >= 0
            finished = self._reaction_until is None and index >= info["frames"]
            if timed_out or finished:
                self._reaction = None
                return self.current(now)
        if info["loop"]:
            index %= info["frames"]
        else:
            index = min(index, info["frames"] - 1)
        return name, index

    def draw(self, x, y, size, now):
        name, index = self.current(now)
        sheet = self._sheets.get(name)
        if sheet is None:
            sheet = self._sheets[name] = image.load("%s/%s.png" % (self._dir, name))
        columns = self._animations[name]["columns"]
        cell = self._cell
        screen.blit(sheet, rect((index % columns) * cell, (index // columns) * cell, cell, cell),
                    rect(x, y, size, size))
        return name


def _plain(text):
    # The wrapping text call treats [..] as markup.
    return text.replace("[", "(").replace("]", ")")


class Screen:
    """State the rest of the app sets; `run` draws it every frame."""

    def __init__(self, ble_name):
        self.ble_name = ble_name
        self.state = service.UNPAIRED
        self.pairing = False       # a phone is connected over BLE
        self.busy = False          # waiting for Muse to act on a button press
        self.title = ""
        self.message = ""
        self.message_at = None
        self.notice = ""
        self.notice_until = 0
        self.dancing_until = None
        badge.mode(HIRES)
        self._small = self._load_font("MonaSans-Medium")
        try:
            self.avatar = Avatar(AVATAR_DIR)
            self.avatar.react("boot")
        except Exception as exc:
            # No sprites on the badge: fall back to the drawn face.
            print("no avatar sprites: %r" % (exc,))
            self.avatar = None

    def _load_font(self, name):
        try:
            return font.load(name)
        except Exception:
            return None

    def show_message(self, text, title=""):
        self.title = _plain(title)
        self.message = _plain(text)
        self.message_at = time.ticks_ms()
        if self.avatar:
            self.avatar.react("speaking", SPEAKING_MS)

    def clear_message(self):
        self.title = self.message = ""
        self.message_at = None

    def pet(self):
        if self.avatar:
            self.avatar.react("happy")

    def dance(self, ms):
        """Dance full screen for `ms`, over any message. False without sprites."""
        if not self.avatar:
            return False
        self.avatar.react("dance", ms)
        self.dancing_until = time.ticks_add(time.ticks_ms(), ms)
        return True

    def flash(self, text, ms=2500):
        self.notice = _plain(text)
        self.notice_until = time.ticks_add(time.ticks_ms(), ms)

    def _text(self, text, area, size, rgb=(255, 255, 255), align=None):
        screen.pen = color.rgb(*rgb)
        if self._small is not None:
            screen.font = self._small
        screen.text(text, area, size, align=align or (image.CENTER, image.MIDDLE),
                    overflow=image.ELLIPSES)

    def _face(self, cx, cy, radius, rgb, now):
        screen.antialias = image.X4
        breathe = 1 + 0.04 * math.sin(now / 600)
        screen.pen = color.rgb(rgb[0] // 4, rgb[1] // 4, rgb[2] // 4)
        screen.shape(shape.circle(cx, cy, radius * breathe + 8))
        screen.pen = color.rgb(*rgb)
        screen.shape(shape.circle(cx, cy, radius * breathe))
        # Blink for a few frames every four seconds.
        blinking = (now % 4000) < 140
        screen.pen = color.rgb(16, 18, 28)
        eye_h = 2 if blinking else radius * 0.34
        for dx in (-0.36, 0.36):
            screen.shape(shape.rounded_rectangle(
                cx + dx * radius - radius * 0.11, cy - radius * 0.12 - eye_h / 2,
                radius * 0.22, eye_h, min(radius * 0.11, eye_h / 2)))

    def _figure(self, x, y, size, now):
        """Draw the avatar, or the plain face without sprites; returns the accent colour."""
        if self.avatar:
            self.avatar.rest = "thinking" if self.busy else _STATE_ANIMATION[self.state]
            return _ACCENT[self.avatar.draw(x, y, size, now)]
        rgb = _STATE_COLOR[self.state]
        self._face(x + size // 2, y + size // 2, size * 0.36, rgb, now)
        return rgb

    def draw(self):
        now = time.ticks_ms()
        w, h = screen.width, screen.height
        screen.pen = color.rgb(0, 0, 0)
        screen.clear()

        dancing = self.dancing_until is not None and time.ticks_diff(self.dancing_until, now) > 0
        if self.message and not dancing:
            self._figure(4, 0, 64, now)
            if self.title:
                self._text(self.title, rect(74, 14, w - 84, 40), 20, (170, 180, 200),
                           (image.LEFT, image.MIDDLE))
            size = 30 if len(self.message) < 60 else 22 if len(self.message) < 160 else 16
            self._text(self.message, rect(14, 66, w - 28, h - 76), size)
        else:
            accent = self._figure((w - 192) // 2, 0, 192, now)
            if dancing:
                status = "Dancing"
            elif self.pairing:
                status = "Pairing with the Muse app"
            elif self.busy:
                status = "Asking Muse"
            else:
                status = _STATE_TEXT[self.state]
            if self.state == service.UNPAIRED and not self.pairing:
                self._text(status, rect(10, 190, w - 20, 24), 18, accent)
                self._text("Muse app > Devices > Add " + self.ble_name,
                           rect(6, 214, w - 12, 22), 13, (170, 180, 200))
            else:
                self._text(status, rect(10, 196, w - 20, 30), 20, accent)

        if self.notice and time.ticks_diff(self.notice_until, now) > 0:
            screen.pen = color.rgb(30, 34, 52)
            screen.shape(shape.rounded_rectangle(20, h - 44, w - 40, 34, 10))
            self._text(self.notice, rect(26, h - 44, w - 52, 34), 15)

    async def run(self, on_button):
        """Draw and poll buttons until cancelled. `on_button(name)` gets A, B, C, UP or DOWN."""
        names = ((BUTTON_A, "A"), (BUTTON_B, "B"), (BUTTON_C, "C"),
                 (BUTTON_UP, "UP"), (BUTTON_DOWN, "DOWN"))
        while True:
            try:
                self.draw()
            except Exception as exc:
                print("draw failed: %r" % (exc,))
            display.update()
            badge.poll()
            for pin, name in names:
                if badge.pressed(pin):
                    on_button(name)
            await asyncio.sleep_ms(FRAME_MS)
