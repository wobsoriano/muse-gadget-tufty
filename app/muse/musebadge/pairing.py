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
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/pairing.py)
# to MicroPython for the Pimoroni Tufty 2350.

"""Device side of Muse Gadget BLE pairing, protocol version 5.

Ported from the Linux Device SDK's `pairing.py`. Community mode only:
`pairing_auth` is "none" and the policy is `confirm_app`, so a valid
client-finished record confirms the session. It protects setup secrets from
passive observers but cannot stop an active man-in-the-middle.

Everything runs on one asyncio loop, so there are no locks.
"""

import hashlib
import json

from . import mcrypto
from .compat import b64url_decode, b64url_encode, monotonic

PAIRING_VERSION = 5
PAIRING_MODEL = "hatch_link"
PAIRING_SUITE = "p256-hkdf-sha256-aes-gcm-v1"
POLICY_APP = "confirm_app"
AUTH_COMMUNITY = "none"

RECORD_LABEL = "hatch-link ble setup v1"
SESSION_ID_LABEL = b"hatch-link session id v1"

CLIENT_FINISHED_TIMEOUT_S = 60
CONFIRMED_TIMEOUT_S = 120
PROVISIONING_TIMEOUT_S = 120

ERROR_INVALID_HELLO = "error_pairing_invalid_hello"
ERROR_DECRYPT = "error_pairing_decrypt"

IDLE = "idle"
WAIT_CLIENT_FINISHED = "wait_client_finished"
READY = "ready"
PROVISIONING = "provisioning"

_P256_POINT_BYTES = 65
_NONCE_BYTES = 16
_SESSION_ID_BYTES = 16
_TAG_BYTES = 16
_MAX_CIPHERTEXT_B64_CHARS = 16384
_TO_DEVICE = 0
_FROM_DEVICE = 1


class PairingError(Exception):
    """A handshake step failed; `status` is the wire error to report."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def parse_counter(text):
    if not isinstance(text, str) or not text or not all("0" <= c <= "9" for c in text):
        raise ValueError("invalid counter")
    if len(text) > 20:
        raise ValueError("counter overflow")
    value = int(text)
    if value >= 1 << 64:
        raise ValueError("counter overflow")
    return value


def build_transcript(device_id, node_id, mac, firmware_version,
                     mobile_pub, device_pub, mobile_nonce, device_nonce):
    """Canonical v5 community transcript; its SHA-256 is the `transcript_hash`."""
    fields = (device_id, node_id, mac, firmware_version,
              mobile_pub, device_pub, mobile_nonce, device_nonce)
    if not all(fields):
        raise ValueError("transcript fields must be non-empty")
    return "\n".join([
        "hatch-link-pairing-v%d" % PAIRING_VERSION,
        "version=%d" % PAIRING_VERSION,
        "initiator_role=mobile",
        "responder_role=link",
        "device_id=" + device_id,
        "node_id=" + node_id,
        "mac=" + mac,
        "model=" + PAIRING_MODEL,
        "firmware_version=" + firmware_version,
        "selected_cipher_suite=" + PAIRING_SUITE,
        "pairing_auth=" + AUTH_COMMUNITY,
        "pairing_auth_epoch=0",
        "pairing_policy=" + POLICY_APP,
        "confirm_timeout_seconds=0",
        "mobile_pub=" + mobile_pub,
        "device_pub=" + device_pub,
        "mobile_nonce=" + mobile_nonce,
        "device_nonce=" + device_nonce,
    ])


def derive_session_keys(ecdh_secret, mobile_nonce, device_nonce, transcript_hash):
    """Return (mobile_tx_key, mobile_rx_key, session_id).

    mobile_tx_key decrypts records the device receives and mobile_rx_key
    encrypts records it sends.
    """
    salt = hashlib.sha256(mobile_nonce + device_nonce + transcript_hash).digest()
    session_secret = mcrypto.hkdf(ecdh_secret, salt, RECORD_LABEL.encode(), 32)
    mobile_tx = mcrypto.hkdf_expand(session_secret, b"mobile->device", 32)
    mobile_rx = mcrypto.hkdf_expand(session_secret, b"device->mobile", 32)
    session_id = hashlib.sha256(
        SESSION_ID_LABEL + transcript_hash + ecdh_secret
    ).digest()[:_SESSION_ID_BYTES]
    return mobile_tx, mobile_rx, session_id


def record_nonce(direction, counter):
    return bytes([direction, 0, 0, 0]) + counter.to_bytes(8, "big")


def record_aad(session_id_b64, direction, counter):
    arrow = "m2d" if direction == _TO_DEVICE else "d2m"
    return ("%s|%s|%s|%d" % (RECORD_LABEL, session_id_b64, arrow, counter)).encode()


class PairingSession:
    """One device's pairing state.

    Methods that advance the handshake return a nonzero generation token.
    Deferred work (Wi-Fi joins, provisioning) holds on to it and checks
    `is_current` before acting, so work from an abandoned attempt cannot act
    on a newer one.
    """

    def __init__(self, node_id, device_id, mac, firmware_version, sdk_token=None,
                 clock=monotonic, generate_key=mcrypto.p256_generate,
                 random_bytes=mcrypto.random_bytes):
        self._node_id = node_id
        self._device_id = device_id
        self._mac = mac
        self._firmware_version = firmware_version or "unknown"
        self._sdk_token = sdk_token
        self._clock = clock
        self._generate_key = generate_key
        self._random_bytes = random_bytes
        self._generation = 0
        self._next_key = None
        self.reset()

    def prepare_key(self):
        """Generate the next session's P-256 key ahead of the hello.

        Key generation is the slow half of the handshake on a microcontroller.
        Doing it while the phone is still connecting keeps `pairing_ready`
        inside the app's timeout.
        """
        if self._next_key is None:
            self._next_key = self._generate_key()

    def device_info(self):
        return {
            "device_id": self._device_id,
            "mac": self._mac,
            "model": PAIRING_MODEL,
            "pairing_protocol": PAIRING_VERSION,
            "pairing_auth": AUTH_COMMUNITY,
            "pairing_auth_epoch": 0,
            "pairing_policy": POLICY_APP,
        }

    @property
    def state(self):
        self._expire()
        return self._state

    @property
    def confirmed(self):
        return self.state in (READY, PROVISIONING)

    def is_current(self, generation):
        return generation != 0 and generation == self._generation

    def reset(self):
        self._generation += 1
        self._clear()

    def handle_hello(self, message):
        """Start a session from `pairing_client_hello`; returns `pairing_ready`."""
        version = message.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, (int, float))
            or version != PAIRING_VERSION
            or message.get("pairing_auth") != AUTH_COMMUNITY
            or message.get("pairing_policy") != POLICY_APP
        ):
            raise PairingError(ERROR_INVALID_HELLO)

        self.reset()
        try:
            mobile_pub = b64url_decode(message.get("mobile_pub"))
            mobile_nonce = b64url_decode(message.get("mobile_nonce"))
            if (
                len(mobile_pub) != _P256_POINT_BYTES
                or mobile_pub[0] != 0x04
                or len(mobile_nonce) != _NONCE_BYTES
            ):
                raise ValueError("invalid hello key material")
            self.prepare_key()
            device_private, device_pub = self._next_key
            self._next_key = None
            ecdh_secret = mcrypto.p256_ecdh(device_private, mobile_pub)
        except ValueError:
            raise PairingError(ERROR_INVALID_HELLO)

        device_nonce = self._random_bytes(_NONCE_BYTES)
        transcript = build_transcript(
            self._device_id, self._node_id, self._mac, self._firmware_version,
            b64url_encode(mobile_pub), b64url_encode(device_pub),
            b64url_encode(mobile_nonce), b64url_encode(device_nonce),
        )
        transcript_hash = hashlib.sha256(transcript.encode()).digest()
        rx_key, tx_key, session_id = derive_session_keys(
            ecdh_secret, mobile_nonce, device_nonce, transcript_hash,
        )
        self._rx = mcrypto.AESGCM(rx_key)
        self._tx = mcrypto.AESGCM(tx_key)
        self._session_id_b64 = b64url_encode(session_id)
        self._state = WAIT_CLIENT_FINISHED
        self._deadline = self._clock() + CLIENT_FINISHED_TIMEOUT_S
        return {
            "type": "pairing_ready",
            "version": PAIRING_VERSION,
            "device_id": self._device_id,
            "node_id": self._node_id,
            "mac": self._mac,
            "model": PAIRING_MODEL,
            "firmware_version": self._firmware_version,
            "pairing_auth": AUTH_COMMUNITY,
            "pairing_auth_epoch": 0,
            "pairing_policy": POLICY_APP,
            "device_pub": b64url_encode(device_pub),
            "device_nonce": b64url_encode(device_nonce),
            "transcript_hash": b64url_encode(transcript_hash),
            "session_id": self._session_id_b64,
        }

    def decrypt(self, envelope):
        """Open one mobile-to-device `pairing_encrypted` record.

        Any failure (wrong session, skipped or replayed counter, bad tag,
        expiry) clears the session, as the firmware does.
        """
        if self._expire() or self._state == IDLE:
            self.reset()
            raise PairingError(ERROR_DECRYPT)
        try:
            if envelope.get("session_id") != self._session_id_b64:
                raise ValueError("wrong session")
            counter = parse_counter(envelope.get("counter"))
            if counter != self._rx_counter:
                raise ValueError("unexpected counter")
            ciphertext = b64url_decode(envelope.get("ciphertext"), _MAX_CIPHERTEXT_B64_CHARS)
            tag = b64url_decode(envelope.get("tag"))
            if len(tag) != _TAG_BYTES:
                raise ValueError("invalid tag length")
            plaintext = self._rx.decrypt(
                record_nonce(_TO_DEVICE, counter),
                ciphertext + tag,
                record_aad(self._session_id_b64, _TO_DEVICE, counter),
            ).decode()
        except (ValueError, mcrypto.InvalidTag):
            self.reset()
            raise PairingError(ERROR_DECRYPT)
        self._rx_counter += 1
        return plaintext

    def handle_client_finished(self, command):
        """Confirm the session after the first decrypted record.

        Returns the new generation, or 0 (after clearing the session) if the
        record is not exactly the first one and `pairing_client_finished`.
        """
        ok = (
            command == {"action": "pairing_client_finished"}
            and not self._expire()
            and self._state == WAIT_CLIENT_FINISHED
            and self._rx_counter == 1
        )
        if not ok:
            self.reset()
            return 0
        self._generation += 1
        self._state = READY
        self._deadline = self._clock() + CONFIRMED_TIMEOUT_S
        return self._generation

    def mark_provisioning(self):
        if not self._expire() and self._state == READY:
            self._generation += 1
            self._state = PROVISIONING
            self._deadline = self._clock() + PROVISIONING_TIMEOUT_S
        return self._generation if self._state == PROVISIONING else 0

    def provisioning_valid(self, generation):
        return (
            generation != 0
            and generation == self._generation
            and not self._expire()
            and self._state == PROVISIONING
        )

    def extend_provisioning(self, generation):
        valid = self.provisioning_valid(generation)
        if valid:
            self._deadline = self._clock() + PROVISIONING_TIMEOUT_S
        return valid

    def encrypt_json(self, plaintext, generation=0):
        """Seal a device-to-mobile record.

        Returns None when there is no active session, or when a nonzero
        generation no longer matches (the work it belongs to is stale).
        """
        if (
            (generation and generation != self._generation)
            or self._expire()
            or self._state == IDLE
        ):
            return None
        counter = self._tx_counter
        sealed = self._tx.encrypt(
            record_nonce(_FROM_DEVICE, counter),
            plaintext.encode(),
            record_aad(self._session_id_b64, _FROM_DEVICE, counter),
        )
        self._tx_counter += 1
        return {
            "type": "pairing_encrypted",
            "session_id": self._session_id_b64,
            "counter": str(counter),
            "ciphertext": b64url_encode(sealed[:-_TAG_BYTES]),
            "tag": b64url_encode(sealed[-_TAG_BYTES:]),
        }

    def encrypt_status(self, status, generation=0):
        message = {"type": "status", "status": status}
        # Apps read only type and status, so older ones ignore the token.
        if self._sdk_token and status == "pairing_confirmed":
            message["sdk_token"] = self._sdk_token
        return self.encrypt_json(json.dumps(message, separators=(",", ":")), generation)

    def _clear(self):
        self._state = IDLE
        self._deadline = 0
        self._rx = None
        self._tx = None
        self._session_id_b64 = ""
        self._rx_counter = 0
        self._tx_counter = 0

    def _expire(self):
        if self._state == IDLE or self._clock() <= self._deadline:
            return False
        # Drop the keys but keep the generation, so the owner of the expired
        # session can still recognise it and close the connection.
        self._clear()
        return True
