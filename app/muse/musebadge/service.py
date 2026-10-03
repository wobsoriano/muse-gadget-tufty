# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/service.py)
# to MicroPython for the Pimoroni Tufty 2350.

"""Keep a paired badge connected to its Muse.

Ported from the Linux Device SDK's `service.py`. Each round fetches the leased
VMs with the device token (which also yields a fresh per-VM bearer), connects
to the default VM and serves commands until the connection ends. Failures back
off exponentially, and a session that stayed up for a while resets the
backoff. The device token is rotated before it expires, and immediately if the
API rejects it.
"""

import asyncio
import time

from . import api, link
from .compat import monotonic
from .setup import ProvisionFailed
from .store import PAIRING_FILE

DEFAULT_NOISE_HOST = "hatch.metaaivm.com"
BACKOFF_BASE_S = 2
BACKOFF_MAX_S = 60
AUTH_BACKOFF_MIN_S = 15
HEALTHY_SESSION_S = 30
UNPAIRED_POLL_S = 2
# Device access tokens live about 4 hours; rotate at 3.
TOKEN_REFRESH_AGE_S = 3 * 3600
TOKEN_RETRY_S = 300

# What the UI shows. `Service.state` is always one of these.
UNPAIRED = "unpaired"
OFFLINE = "offline"
CONNECTING = "connecting"
CONNECTED = "connected"


class Service:
    """`http` is `api`'s request coroutine. `connect(url, headers)` opens a
    WebSocket. `run_command` and `commands` are the device's command handler
    and its specs. `online()` says whether Wi-Fi is up.
    """

    def __init__(self, identity, store, http, connect, run_command, commands,
                 version, display_name, user_agent, sdk_token=None,
                 online=lambda: True, ssid=lambda: "", log=print, sleep=asyncio.sleep):
        self.identity = identity
        self.state = UNPAIRED
        self._store = store
        self._http = http
        self._connect = connect
        self._run_command = run_command
        self._commands = commands
        self._version = version
        self._display_name = display_name
        self._user_agent = user_agent
        self._sdk_token = sdk_token
        self._online = online
        self._ssid = ssid
        self._log = log
        self._sleep = sleep
        self._last_refresh_attempt = None
        # The SDK token reaches Muse only in refresh bodies until apps forward
        # it at mint, so each start with a token attempts one refresh to
        # report it.
        self._sdk_token_reported = False
        self._session = None
        self._stopped = False

    def register_params(self):
        return {
            "node_id": self.identity.node_id,
            "display_name": self._display_name,
            # The VM knows these values from the Linux Device SDK. Family
            # "link" must never be used: the server pushes ESP32 firmware
            # updates to every link device.
            "platform": "linux",
            "version": self._version,
            "device_family": "homehub",
            "model_id": "linux",
            "is_wakeup_supported": False,
            "commands_v2": self._commands,
        }

    def _register_with_network(self):
        params = self.register_params()
        ssid = self._ssid()
        if ssid:
            params["metadata"] = {"network_ssid": ssid}
        return params

    @property
    def paired(self):
        return self._store.load(PAIRING_FILE) is not None

    async def stop(self):
        self._stopped = True
        if self._session is not None:
            await self._session.stop()

    async def send_chat(self, message, session_id=None):
        if self._session is None or not self._session.connected:
            return {"ok": False, "status": 0, "response": "not connected"}
        return await self._session.send_chat(message, session_id)

    async def ask(self, message, on_text=None):
        """Send a message and return the Muse's reply text; raises OSError."""
        if self._session is None or not self._session.connected:
            raise OSError("not connected")
        return await self._session.ask(message, on_text=on_text)

    async def verify_credentials(self, credentials):
        """Check a freshly provisioned token; returns the record to persist."""
        api_url = credentials["api_url"] if credentials["api_url"].startswith("https://") else ""
        api_url_v2 = credentials["api_url_v2"] if credentials["api_url_v2"].startswith("https://") else ""
        vms, status = await api.fetch_vms(
            self._http, credentials["access_token"], api.api_root(api_url_v2),
            self._user_agent, self._log)
        if not vms:
            self._log("device token check failed (HTTP %s)" % status)
            raise ProvisionFailed("auth_failed")
        return {
            "access_token": credentials["access_token"],
            "refresh_token": credentials["refresh_token"],
            "token_type": "device",
            "username": credentials["username"],
            "api_url": api_url,
            "api_url_v2": api_url_v2,
            "noise_host": credentials["noise_host"],
            "access_token_saved_at": int(time.time()),
        }

    async def run(self):
        failures = 0
        floor = 0
        while not self._stopped:
            pairing = self._store.load(PAIRING_FILE)
            if not pairing:
                self.state = UNPAIRED
                await self._sleep(UNPAIRED_POLL_S)
                continue
            if not self._online():
                self.state = OFFLINE
                await self._sleep(UNPAIRED_POLL_S)
                continue
            self.state = CONNECTING
            pairing = await self._maybe_refresh(pairing)
            if pairing is None:
                await self._sleep(TOKEN_RETRY_S if self.paired else UNPAIRED_POLL_S)
                continue
            root = api.api_root(pairing.get("api_url_v2", ""))
            vms, status = await api.fetch_vms(
                self._http, pairing["access_token"], root, self._user_agent, self._log)
            if status == 401:
                self._log("device token rejected by the API; refreshing")
                if await self._maybe_refresh(pairing, force=True) is None:
                    await self._sleep(TOKEN_RETRY_S if self.paired else UNPAIRED_POLL_S)
                continue
            vm = None
            for candidate in vms:
                if candidate["is_default"]:
                    vm = candidate
                    break
            if vm is None and vms:
                vm = vms[0]
            if vm is not None:
                outcome, lasted = await self._serve(vm, pairing)
                if self._stopped:
                    return
                if outcome == link.UNPAIRED:
                    self._store.delete(PAIRING_FILE)
                    self._log("pairing removed by the Muse")
                    continue
                if lasted >= HEALTHY_SESSION_S:
                    failures = 0
                    floor = 0
                if outcome in (link.AUTH_REJECTED, link.FORBIDDEN):
                    floor = AUTH_BACKOFF_MIN_S
            self.state = CONNECTING
            delay = max(min(BACKOFF_BASE_S * (2 ** failures), BACKOFF_MAX_S), floor)
            failures += 1
            self._log("reconnecting in %ds" % delay)
            await self._sleep(delay)

    async def _serve(self, vm, pairing):
        session = link.LinkSession(
            pairing.get("noise_host") or DEFAULT_NOISE_HOST,
            vm["vm_id"] or vm["vm_name"],
            vm["vm_auth_token"],
            self._register_with_network(),
            self._run_command,
            self._connect,
            log=self._log,
        )
        self._log("connecting to %s" % (vm["vm_name"] or vm["vm_id"]))
        self._session = session
        watcher = asyncio.create_task(self._watch_registration(session))
        try:
            outcome = await session.run()
        except Exception as exc:
            self._log("session failed: %r" % (exc,))
            outcome = link.CLOSED
        finally:
            watcher.cancel()
            self._session = None
        lasted = monotonic() - session.registered_at if session.registered_at else 0
        self._log("session ended: %s after %ds registered" % (outcome, lasted))
        return outcome, lasted

    async def _watch_registration(self, session):
        while not session.connected:
            await asyncio.sleep(0.2)
        self.state = CONNECTED

    async def _maybe_refresh(self, pairing, force=False):
        """Return current pairing, rotating tokens first if they are due.

        Returns None only when a due refresh failed and the old token should
        not be used yet. The pairing file is removed when the pairing itself
        has been revoked.
        """
        age = time.time() - pairing.get("access_token_saved_at", 0)
        report_due = bool(self._sdk_token) and not self._sdk_token_reported
        # A clock that has not been set yet makes the age meaningless.
        due = force or age >= TOKEN_REFRESH_AGE_S or age < 0
        if not due and not report_due:
            return pairing
        now = monotonic()
        if (not force and self._last_refresh_attempt is not None
                and now - self._last_refresh_attempt < TOKEN_RETRY_S):
            return pairing
        self._last_refresh_attempt = now
        if report_due:
            self._sdk_token_reported = True
            self._log("refreshing device token to report the SDK token")
        tokens, status = await api.refresh_device_token(
            self._http, pairing["refresh_token"], self.identity.node_id,
            api.api_root(pairing.get("api_url_v2", "")), self._user_agent,
            self._sdk_token, self._log)
        if tokens:
            pairing = dict(pairing)
            pairing["access_token"] = tokens["access_token"]
            pairing["refresh_token"] = tokens["refresh_token"]
            pairing["access_token_saved_at"] = int(time.time())
            self._store.save(PAIRING_FILE, pairing)
            self._log("device token rotated")
            return pairing
        if not due:
            # Only reporting the SDK token: nothing has rejected the current
            # token, so a refusal here must never unpair the device.
            self._log("SDK token report refresh failed (HTTP %s); keeping the pairing" % status)
            return pairing
        if status == 401:
            self._store.delete(PAIRING_FILE)
            self._log("pairing revoked; set the badge up again in the Muse app")
            return None
        # Transient failure: keep using the current token while it still works.
        return None if force else pairing
