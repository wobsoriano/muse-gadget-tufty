"""Wi-Fi for the badge: join, scan, and stay connected. MicroPython only."""

import asyncio
import time

import network

from .store import WIFI_FILE

CURRENT_CONNECTION_LABEL = "Use current connection"
JOIN_TIMEOUT_S = 25
MAX_SCAN_ENTRIES = 12
REJOIN_MAX_S = 300


class Wifi:
    def __init__(self, store, log=print):
        self._store = store
        self._log = log
        self._joining = False
        self.paused = False
        self.wlan = network.WLAN(network.STA_IF)

    def is_online(self):
        return self.wlan.isconnected()

    def ssid(self):
        try:
            return self.wlan.config("ssid") or ""
        except Exception:
            return ""

    def current_connection_entry(self):
        # Marked open so the app skips the password field: the badge is
        # already on this network and ignores what comes back.
        return {"ssid": self.ssid() or CURRENT_CONNECTION_LABEL, "rssi": -40, "secure": False}

    async def scan(self):
        self.wlan.active(True)
        best = {}
        for entry in self.wlan.scan():
            ssid = entry[0].decode() if isinstance(entry[0], bytes) else entry[0]
            if ssid and (ssid not in best or entry[3] > best[ssid]["rssi"]):
                best[ssid] = {"ssid": ssid, "rssi": entry[3], "secure": entry[4] != 0}
        networks = sorted(best.values(), key=lambda n: -n["rssi"])
        return networks[:MAX_SCAN_ENTRIES]

    async def join(self, ssid, password, remember=True):
        if self._joining:
            return False
        self._joining = True
        try:
            self.wlan.active(True)
            if self.wlan.isconnected():
                self.wlan.disconnect()
                await asyncio.sleep(0.5)
            self._log("joining Wi-Fi %s" % ssid)
            self.wlan.connect(ssid, password or None)
            deadline = time.ticks_add(time.ticks_ms(), JOIN_TIMEOUT_S * 1000)
            while time.ticks_diff(deadline, time.ticks_ms()) > 0:
                if self.wlan.isconnected():
                    if remember:
                        self._store.save(WIFI_FILE, {"ssid": ssid, "password": password})
                    self._log("Wi-Fi connected: %s" % self.wlan.ifconfig()[0])
                    return True
                if self.wlan.status() < 0:
                    break
                await asyncio.sleep(0.25)
            self._log("Wi-Fi join failed (status %d)" % self.wlan.status())
            return False
        finally:
            self._joining = False

    def _saved(self):
        saved = self._store.load(WIFI_FILE)
        if saved and saved.get("ssid"):
            return saved["ssid"], saved.get("password", "")
        try:
            import secrets
            if secrets.WIFI_SSID:
                return secrets.WIFI_SSID, secrets.WIFI_PASSWORD
        except Exception:
            pass
        return None

    async def keep_connected(self):
        """Rejoin the saved network whenever the link drops, and set the clock.

        Failed joins back off, and none are tried while `self.paused` is set:
        a join attempt takes the radio away from a setup in progress.
        """
        clock_set = False
        wait_s = 0
        while True:
            if self.wlan.isconnected():
                wait_s = 0
                if not clock_set:
                    clock_set = self._set_clock()
            elif not self._joining and not self.paused:
                saved = self._saved()
                if saved and not await self.join(saved[0], saved[1], remember=False):
                    wait_s = min(max(wait_s * 2, 10), REJOIN_MAX_S)
            await asyncio.sleep(max(wait_s, 5))

    def _set_clock(self):
        # TLS certificate checks need a real date.
        if time.gmtime()[0] >= 2025:
            return True
        try:
            import ntptime
            ntptime.settime()
            self._log("clock set from NTP")
            return True
        except Exception as exc:
            self._log("NTP failed: %r" % (exc,))
            return False
