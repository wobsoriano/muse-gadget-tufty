"""The Muse gadget app for the Tufty 2350. MicroPython only.

Wires the screen, Wi-Fi, BLE setup and the Muse session together on one
asyncio loop.
"""

import asyncio
import gc
import os
import ssl
import time

import machine

from . import __version__, mcrypto, net, pairing, service, setup, store
from .ble import BleServer
from .ui import Screen
from .wifi import Wifi

STATE_DIR = "/state/muse"
SDK_TOKEN_FILE = "sdk_token.json"
LOG_FILE = "log.txt"
LOG_MAX_BYTES = 24 * 1024
DISPLAY_NAME = "Tufty Badge"
DANCE_S = 8
USER_AGENT = "musebadge/%s (Pimoroni Tufty 2350; MicroPython rp2)" % __version__
CA_FILE = __file__.rsplit("/", 1)[0] + "/digicert_global_root_g2.der"

COMMANDS = {
    "badge.show_message": {
        "description": (
            "Show a short text message on the screen of the badge the user is "
            "wearing. The screen is 320x240, so keep it under about 200 characters."
        ),
        "required": {
            "text": {"type": "string", "description": "The message to show."},
        },
        "optional": {
            "title": {"type": "string", "description": "A short heading above the message."},
        },
    },
    "badge.clear": {
        "description": "Clear the message on the badge and go back to the status face.",
        "required": {},
        "optional": {},
    },
    "badge.dance": {
        "description": (
            "Make the Muse avatar on the user's badge screen dance. The avatar is you, "
            "so call this whenever the user says 'make Muse dance', 'dance', or asks "
            "you, their badge or their avatar to dance or celebrate."
        ),
        "required": {},
        "optional": {
            "seconds": {"type": "number", "description": "How long to dance, 2 to 60. Default 8."},
        },
    },
    "badge.set_lights": {
        "description": "Set the brightness of the four LEDs on the back of the badge.",
        "required": {
            "brightness": {"type": "number", "description": "0.0 (off) to 1.0 (full)."},
        },
        "optional": {},
    },
    "device.health": {
        "description": "Badge status: uptime, free memory, battery, Wi-Fi, software version.",
        "required": {},
        "optional": {},
    },
}

# The reply is shown on the badge, so each prompt asks for something short.
BUTTON_PROMPTS = {
    "A": "Tell me a one line joke about databases. Reply with only the joke, under 120 characters.",
    "C": "Give me one line of encouragement for someone demoing on stage. Reply with only that line, under 120 characters.",
}


def _tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    with open(CA_FILE, "rb") as f:
        context.load_verify_locations(cadata=f.read())
    return context


class App:
    def __init__(self):
        self.started = time.ticks_ms()
        self.files = store.Store(STATE_DIR)
        self.identity = store.load_identity(self.files)
        self.sdk_token = (self.files.load(SDK_TOKEN_FILE) or {}).get("token")
        self.screen = Screen(self.identity.ble_name)
        self.wifi = Wifi(self.files, self.log)
        self.tls = _tls_context()
        self.service = service.Service(
            self.identity, self.files, self.http, self.connect, self.run_command,
            COMMANDS, __version__, DISPLAY_NAME, USER_AGENT, sdk_token=self.sdk_token,
            online=self.wifi.is_online, ssid=self.wifi.ssid, log=self.log)
        self.pairing = pairing.PairingSession(
            self.identity.node_id, self.identity.device_id, self.identity.mac,
            __version__, sdk_token=self.sdk_token)
        self.ble = BleServer(
            self.identity.ble_name, self.on_ble_write, self.on_ble_connect,
            self.on_ble_disconnect, self.log)
        self.controller = setup.SetupController(
            self.pairing, self.identity, __version__, self.ble, self.wifi,
            self.service.verify_credentials,
            lambda record: self.files.save(store.PAIRING_FILE, record),
            on_complete=self.on_setup_complete, log=self.log)

    def log(self, message):
        line = "[muse %d] %s" % (time.ticks_diff(time.ticks_ms(), self.started) // 1000, message)
        print(line)
        # Kept on flash too, because USB serial is the only other way to see
        # what happened and attaching to it interrupts the app.
        try:
            path = STATE_DIR + "/" + LOG_FILE
            try:
                if os.stat(path)[6] > LOG_MAX_BYTES:
                    os.rename(path, path + ".old")
            except OSError:
                pass
            with open(path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    async def http(self, method, url, headers, body):
        return await net.http_request(method, url, headers, body, ssl=self.tls)

    async def connect(self, url, headers):
        headers = dict(headers)
        headers["User-Agent"] = USER_AGENT
        return await net.ws_connect(url, headers, ssl=self.tls)

    def on_ble_write(self, packet):
        self.controller.on_write(packet)

    def on_ble_connect(self):
        self.screen.pairing = True
        self.wifi.paused = True
        # The slow half of the handshake, done while the phone discovers
        # services, so pairing_ready is not late.
        self.pairing.prepare_key()

    def on_ble_disconnect(self):
        self.screen.pairing = False
        self.wifi.paused = False
        self.controller.on_disconnect()

    def on_setup_complete(self):
        self.screen.flash("Paired with Muse")

    async def run_command(self, command, params, timeout_ms):
        if command == "badge.show_message":
            text = params.get("text")
            if not isinstance(text, str) or not text:
                return {"ok": False, "error": "text is required"}
            title = params.get("title")
            self.screen.show_message(text, title if isinstance(title, str) else "")
            return {"ok": True, "payload": {"shown": True}}
        if command == "badge.clear":
            self.screen.clear_message()
            return {"ok": True, "payload": {"cleared": True}}
        if command == "badge.dance":
            try:
                seconds = min(60.0, max(2.0, float(params.get("seconds", DANCE_S))))
            except (TypeError, ValueError):
                return {"ok": False, "error": "seconds must be a number from 2 to 60"}
            if not self.screen.dance(int(seconds * 1000)):
                return {"ok": False, "error": "this badge has no avatar animations installed"}
            return {"ok": True, "payload": {"dancing_for_s": seconds}}
        if command == "badge.set_lights":
            try:
                level = min(1.0, max(0.0, float(params.get("brightness"))))
            except (TypeError, ValueError):
                return {"ok": False, "error": "brightness must be a number from 0 to 1"}
            badge.caselights(level)
            return {"ok": True, "payload": {"brightness": level}}
        if command == "device.health":
            gc.collect()
            return {"ok": True, "payload": {
                "uptime_s": time.ticks_diff(time.ticks_ms(), self.started) // 1000,
                "mem_free": gc.mem_free(),
                "battery_percent": badge.battery_level(),
                "charging": badge.is_charging(),
                "wifi_ssid": self.wifi.ssid(),
                "version": __version__,
                "board": "Pimoroni Tufty 2350",
            }}
        return {"ok": False, "error": "unsupported command: " + command}

    def on_button(self, name):
        if name == "B":
            self.screen.clear_message()
        elif name == "UP":
            self.screen.pet()
        elif name == "DOWN":
            self.screen.dance(DANCE_S * 1000)
        elif name in BUTTON_PROMPTS:
            asyncio.create_task(self.ask(BUTTON_PROMPTS[name]))

    async def ask(self, prompt):
        """Send a button's prompt to the Muse and put its answer on the screen."""
        if self.service.state != service.CONNECTED:
            self.screen.flash("Not connected to Muse")
            return
        if self.screen.busy:
            return
        self.screen.busy = True
        before = self.screen.message_at
        try:
            reply = await self.service.ask(prompt)
        except Exception as exc:
            self.log("ask failed: %r" % (exc,))
            self.screen.flash("Muse did not answer")
            return
        finally:
            self.screen.busy = False
        # The Muse may have answered by calling badge.show_message itself.
        if self.screen.message_at == before:
            self.screen.show_message(reply, "Muse")

    async def mirror_state(self):
        while True:
            self.screen.state = self.service.state
            await asyncio.sleep(0.2)

    async def setup_window(self):
        """Advertise over BLE only while the badge is not paired."""
        while True:
            if self.service.paired:
                await asyncio.sleep(2)
                continue
            server = asyncio.create_task(self.ble.run())
            while not self.service.paired:
                await asyncio.sleep(1)
            # Let the app read the final status before the link drops.
            await asyncio.sleep(1.5)
            server.cancel()

    async def run(self):
        tasks = [
            asyncio.create_task(self.wifi.keep_connected()),
            asyncio.create_task(self.controller.run()),
            asyncio.create_task(self.setup_window()),
            asyncio.create_task(self.service.run()),
            asyncio.create_task(self.mirror_state()),
        ]
        try:
            await self.screen.run(self.on_button)
        finally:
            for task in tasks:
                task.cancel()


def main():
    # The badge firmware arms an 8 second hardware watchdog. Wi-Fi joins, TLS
    # handshakes and key agreement can each hold the loop for seconds, so a
    # timer keeps it fed instead of the frame loop.
    watchdog = machine.WDT(timeout=8000)
    timer = machine.Timer(period=1000, callback=lambda t: watchdog.feed())
    mcrypto.set_yield_hook(watchdog.feed)
    try:
        asyncio.run(App().run())
    finally:
        timer.deinit()
