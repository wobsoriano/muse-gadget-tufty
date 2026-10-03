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
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/config.py and identity.py)
# to MicroPython for the Pimoroni Tufty 2350.

"""Persistent device state: identity, pairing credentials and Wi-Fi."""

import json
import os

IDENTITY_FILE = "identity.json"
PAIRING_FILE = "pairing.json"
WIFI_FILE = "wifi.json"

NODE_ID_PREFIX = "homelink-"
BLE_NAME_PREFIX = "MuseGadget"


class Store:
    def __init__(self, directory):
        self._dir = directory.rstrip("/")
        try:
            os.mkdir(self._dir)
        except OSError:
            pass

    def _path(self, name):
        return self._dir + "/" + name

    def load(self, name):
        try:
            with open(self._path(name)) as f:
                data = json.loads(f.read())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, name, data):
        # Write then rename, so a reset mid-write leaves the old file intact.
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(data))
        os.rename(tmp, self._path(name))

    def delete(self, name):
        try:
            os.remove(self._path(name))
        except OSError:
            pass


class Identity:
    """A random, locally administered MAC-shaped value, generated once.

    The node id and the BLE name end in the same six hex digits, which is how
    the Muse app matches the gadget it scanned to the one that answered.
    """

    def __init__(self, mac):
        self.mac = mac
        self.suffix = mac.replace(":", "")[-6:]
        self.node_id = NODE_ID_PREFIX + self.suffix
        self.device_id = "hatch-link:" + mac
        self.ble_name = BLE_NAME_PREFIX + self.suffix.upper()


def _valid_mac(mac):
    parts = mac.split(":") if isinstance(mac, str) else ()
    return len(parts) == 6 and all(
        len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts)


def load_identity(store, random_bytes=os.urandom):
    stored = store.load(IDENTITY_FILE) or {}
    if _valid_mac(stored.get("mac")):
        return Identity(stored["mac"])
    octets = bytearray(random_bytes(6))
    octets[0] = (octets[0] & 0xFC) | 0x02  # unicast, locally administered
    identity = Identity(":".join("%02x" % b for b in octets))
    store.save(IDENTITY_FILE, {"mac": identity.mac})
    return identity
