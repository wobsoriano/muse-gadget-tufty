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
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/ble_setup.py)
# to MicroPython for the Pimoroni Tufty 2350.

"""BLE setup commands: the protocol logic behind the GATT characteristics.

Ported from the Linux Device SDK's `ble_setup.py`, with threads replaced by
one asyncio consumer task. Unlike the Linux client, which only sets up when it
is already online, this one joins the Wi-Fi network the app sends when it
differs from the one the badge is on.

The transport provides `async send_packets(packets)`, `mtu()` and
`disconnect(delay_s)`. The network provides `is_online()`,
`current_connection_entry()`, `async scan()` and `async join(ssid, password)`.
"""

import asyncio
import json

from .ble_framing import ChunkAssembler, encode_chunks
from .compat import compact_json
from .pairing import PairingError

SENSITIVE_ACTIONS = (
    "provision", "provision_v2", "wifi_scan", "ota", "device.ota",
    "unpair", "set_wifi", "set_auth",
)
PLAINTEXT_STATUSES = (
    "error_encryption_required",
    "error_pairing_invalid_hello",
    "error_pairing_unavailable",
    "error_pairing_decrypt",
)
DISCONNECT_AFTER_ERROR_S = 0.3


class ProvisionFailed(Exception):
    """Setup could not finish; `status` is what the app is told."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


class SetupController:
    """Handles one BLE setup client at a time.

    `provision(credentials)` is a coroutine that verifies the credentials and
    returns the record to persist, or raises ProvisionFailed. `save(record)`
    persists it. `on_complete()` runs once setup has finished.
    """

    def __init__(self, pairing, identity, version, transport, network,
                 provision, save, on_complete=lambda: None, log=print):
        self._pairing = pairing
        self._identity = identity
        self._version = version
        self._transport = transport
        self._network = network
        self._provision = provision
        self._save = save
        self._on_complete = on_complete
        self._log = log
        self._assembler = ChunkAssembler()
        self._inbox = []
        self._wake = asyncio.Event()
        self._tx_lock = asyncio.Lock()
        self._plaintext_blocked = False
        self._provisioning = False

    def on_write(self, packet):
        message = self._assembler.feed(packet)
        if message is not None:
            self._inbox.append(message)
            self._wake.set()

    def on_disconnect(self):
        self._log("BLE client disconnected; clearing pairing session")
        self._assembler.reset()
        self._plaintext_blocked = False
        self._pairing.reset()

    async def run(self):
        """Handle queued messages one at a time, in arrival order."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._inbox:
                message = self._inbox.pop(0)
                try:
                    await self.handle_message(message)
                except Exception as exc:
                    self._log("setup command failed: %r" % (exc,))

    async def handle_message(self, raw, decrypted=False):
        try:
            command = json.loads(raw.decode())
        except (ValueError, UnicodeError):
            await self.send_status("error_invalid_command")
            return
        if not isinstance(command, dict):
            await self.send_status("error_invalid_command")
            return
        action = command.get("action")
        action = action if isinstance(action, str) else ""
        self._log("RX action: %s%s" % (action or "?", " (encrypted)" if decrypted else ""))

        if not decrypted and action == "pairing_client_hello":
            await self._handle_hello(command)
        elif not decrypted and action == "pairing_encrypted":
            await self._handle_record(command)
        elif action == "get_device_info":
            # Public metadata, safe in plaintext at any point. Apps re-read it
            # when they restart a handshake on the same connection.
            await self.send_json(self.device_info())
        elif not decrypted and self._plaintext_blocked:
            self._log("plaintext command ignored after pairing started: " + action)
        elif not decrypted and action in SENSITIVE_ACTIONS:
            await self.send_status("error_encryption_required")
        elif decrypted and action == "pairing_client_finished":
            await self._handle_client_finished(command)
        elif decrypted and action in SENSITIVE_ACTIONS and not self._pairing.confirmed:
            await self.send_status("error_pairing_confirm_required")
        elif decrypted and action == "wifi_scan":
            await self._handle_wifi_scan()
        elif decrypted and action == "provision_v2":
            await self._handle_provision(command)
        else:
            await self.send_status("error_unknown_action")

    def device_info(self):
        info = {
            "type": "device_info",
            "node_id": self._identity.node_id,
            "version": self._version,
            "build_sha": "",
            "network_ready": self._network.is_online(),
        }
        info.update(self._pairing.device_info())
        return info

    async def _handle_hello(self, command):
        try:
            ready = self._pairing.handle_hello(command)
        except PairingError as err:
            await self.send_status(err.status)
            return
        self._plaintext_blocked = True
        await self.send_json(ready)

    async def _handle_record(self, envelope):
        try:
            plaintext = self._pairing.decrypt(envelope)
        except PairingError as err:
            await self.send_status(err.status)
            self._transport.disconnect(DISCONNECT_AFTER_ERROR_S)
            return
        await self.handle_message(plaintext.encode(), decrypted=True)

    async def _handle_client_finished(self, command):
        generation = self._pairing.handle_client_finished(command)
        if not generation:
            await self.send_status("error_pairing_decrypt")
            self._transport.disconnect(DISCONNECT_AFTER_ERROR_S)
            return
        self._log("pairing confirmed (app consent)")
        await self.send_status("pairing_confirmed", generation)

    async def _handle_wifi_scan(self):
        networks = await self._network.scan()
        if self._network.is_online():
            # The network the badge is on goes first, marked open so the app
            # skips the password. The rest stay listed, because being joined
            # is no proof of internet: a hotel portal joins and then blocks.
            current = self._network.current_connection_entry()
            networks = [current] + [n for n in networks if n["ssid"] != current["ssid"]]
        await self.send_encrypted_json({"type": "wifi_scan_result", "networks": networks})

    async def _handle_provision(self, command):
        def text(key):
            value = command.get(key)
            return value if isinstance(value, str) else ""

        if (
            not isinstance(command.get("ssid"), str)
            or not isinstance(command.get("password"), str)
            or not text("access_token")
            or not text("refresh_token")
            or text("token_type") != "device"
        ):
            await self.send_status("error_missing_credentials")
            return
        if self._provisioning:
            await self.send_status("error_operation_in_progress")
            return
        generation = self._pairing.mark_provisioning()
        if not generation:
            await self.send_status("error_pairing_confirm_required")
            return
        self._provisioning = True
        credentials = {
            key: text(key) for key in
            ("access_token", "refresh_token", "username", "api_url", "api_url_v2", "noise_host")
        }
        # A task, so the consumer keeps answering the app while Wi-Fi joins.
        asyncio.create_task(self._run_provision(
            credentials, command["ssid"], command["password"], generation))

    async def _run_provision(self, credentials, ssid, password, generation):
        try:
            await self.send_status("wifi_connecting", generation)
            current = self._network.current_connection_entry()["ssid"]
            if self._network.is_online() and ssid in ("", current):
                online = True
            else:
                online = await self._network.join(ssid, password)
            if not online:
                # Stay in provisioning so the app can retry, as the firmware does.
                self._pairing.extend_provisioning(generation)
                await self.send_status("wifi_failed", generation)
                return
            await self.send_status("wifi_connected", generation)
            try:
                record = await self._provision(credentials)
                if not self._pairing.provisioning_valid(generation):
                    raise ProvisionFailed("error_storage")
                self._save(record)
            except ProvisionFailed as err:
                self._log("provisioning failed: " + err.status)
                await self.send_status(err.status, generation)
                self._transport.disconnect(0.5)
                return
            await self.send_status("auth_ok", generation)
            self._log("setup complete")
            self._on_complete()
        except Exception as exc:
            self._log("provisioning crashed: %r" % (exc,))
        finally:
            self._provisioning = False

    async def send_status(self, status, generation=0):
        async with self._tx_lock:
            envelope = self._pairing.encrypt_status(status, generation)
            if envelope is not None:
                await self._send(compact_json(envelope))
                self._log("TX status (encrypted #%s): %s" % (envelope["counter"], status))
                return
            if generation or self._plaintext_blocked or status not in PLAINTEXT_STATUSES:
                self._log("TX status suppressed: " + status)
                return
            await self._transport.send_packets([status.encode()])
            self._log("TX status: " + status)

    async def send_json(self, obj):
        async with self._tx_lock:
            await self._send(compact_json(obj))
            self._log("TX %s" % obj.get("type"))

    async def send_encrypted_json(self, obj, generation=0):
        async with self._tx_lock:
            envelope = self._pairing.encrypt_json(compact_json(obj), generation)
            if envelope is None:
                self._log("TX %s suppressed: no session" % obj.get("type"))
                return
            await self._send(compact_json(envelope))
            self._log("TX %s (encrypted #%s)" % (obj.get("type"), envelope["counter"]))

    async def _send(self, text):
        await self._transport.send_packets(encode_chunks(text.encode(), self._transport.mtu()))
