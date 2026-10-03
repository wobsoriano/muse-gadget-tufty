#!/usr/bin/env python3
"""Play the Muse app's side of BLE setup against a real badge, from this Mac.

    uv run --with bleak --with cryptography tools/phone_sim.py [--provision SSID PASSWORD]

Scans for a MuseGadget, pairs with it, and asks for a Wi-Fi scan. With
--provision it also sends provision_v2 with made-up device tokens, which the
real Muse API must reject: the expected ending is wifi_connected, then
auth_failed. That exercises the badge's Wi-Fi join and its TLS path to
api.muse.ai without a Muse account.

The phone side is written from the protocol with the `cryptography` package
and shares no code with the badge.
"""

import asyncio
import hashlib
import json
import os
import sys
import time

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
import base64

SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"
LABEL = "hatch-link ble setup v1"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Phone:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.nonce = os.urandom(16)
        self.pub = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.counter = 0

    def accept(self, ready):
        transcript = "\n".join([
            "hatch-link-pairing-v5", "version=5", "initiator_role=mobile", "responder_role=link",
            "device_id=" + ready["device_id"], "node_id=" + ready["node_id"], "mac=" + ready["mac"],
            "model=hatch_link", "firmware_version=" + ready["firmware_version"],
            "selected_cipher_suite=p256-hkdf-sha256-aes-gcm-v1", "pairing_auth=none",
            "pairing_auth_epoch=0", "pairing_policy=confirm_app", "confirm_timeout_seconds=0",
            "mobile_pub=" + b64(self.pub), "device_pub=" + ready["device_pub"],
            "mobile_nonce=" + b64(self.nonce), "device_nonce=" + ready["device_nonce"],
        ])
        digest = hashlib.sha256(transcript.encode()).digest()
        if b64(digest) != ready["transcript_hash"]:
            sys.exit("FAIL: transcript hash mismatch")
        secret = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), unb64(ready["device_pub"])))
        salt = hashlib.sha256(self.nonce + unb64(ready["device_nonce"]) + digest).digest()
        session = HKDF(hashes.SHA256(), 32, salt, LABEL.encode()).derive(secret)
        self.tx = AESGCM(HKDFExpand(hashes.SHA256(), 32, b"mobile->device").derive(session))
        self.rx = AESGCM(HKDFExpand(hashes.SHA256(), 32, b"device->mobile").derive(session))
        self.session_id = ready["session_id"]

    def seal(self, command):
        counter = self.counter
        self.counter += 1
        sealed = self.tx.encrypt(bytes(4) + counter.to_bytes(8, "big"), json.dumps(command).encode(),
                                 f"{LABEL}|{self.session_id}|m2d|{counter}".encode())
        return {"action": "pairing_encrypted", "session_id": self.session_id, "counter": str(counter),
                "ciphertext": b64(sealed[:-16]), "tag": b64(sealed[-16:])}

    def open(self, envelope):
        counter = int(envelope["counter"])
        return json.loads(self.rx.decrypt(
            bytes([1, 0, 0, 0]) + counter.to_bytes(8, "big"),
            unb64(envelope["ciphertext"]) + unb64(envelope["tag"]),
            f"{LABEL}|{self.session_id}|d2m|{counter}".encode()))


class Assembler:
    def __init__(self):
        self.parts = []
        self.messages = asyncio.Queue()

    def feed(self, _, data):
        data = bytes(data)
        if len(data) < 3 or data[0] != 0xFE:
            self.messages.put_nowait(data)
            return
        if data[1] == 0:
            self.parts = []
        self.parts.append(data[3:])
        if data[1] + 1 == data[2]:
            self.messages.put_nowait(b"".join(self.parts))


async def main():
    provision = None
    if "--provision" in sys.argv:
        i = sys.argv.index("--provision")
        provision = (sys.argv[i + 1], sys.argv[i + 2])

    print("scanning for a MuseGadget...")
    found = await BleakScanner.find_device_by_filter(
        lambda d, adv: (adv.local_name or d.name or "").startswith("MuseGadget"), timeout=20)
    if found is None:
        sys.exit("FAIL: no MuseGadget advertising")
    print("found", found.name, found.address)

    async with BleakClient(found) as client:
        mtu = client.mtu_size
        print("connected, MTU", mtu)
        inbox = Assembler()
        await client.start_notify(TX_UUID, inbox.feed)

        async def send(command):
            data = json.dumps(command, separators=(",", ":")).encode()
            usable = min(mtu - 3, 160) - 3
            chunks = [data[i:i + usable] for i in range(0, len(data), usable)]
            for index, chunk in enumerate(chunks):
                await client.write_gatt_char(
                    RX_UUID, bytes([0xFE, index, len(chunks)]) + chunk, response=True)

        async def reply(timeout=30):
            return await asyncio.wait_for(inbox.messages.get(), timeout)

        await send({"action": "get_device_info"})
        info = json.loads(await reply())
        print("device_info:", {k: info[k] for k in ("node_id", "model", "pairing_protocol",
                                                     "pairing_policy", "network_ready", "version")})

        phone = Phone()
        started = time.monotonic()
        await send({"action": "pairing_client_hello", "version": 5, "pairing_auth": "none",
                    "pairing_policy": "confirm_app", "mobile_pub": b64(phone.pub),
                    "mobile_nonce": b64(phone.nonce)})
        ready = json.loads(await reply())
        print("pairing_ready in %.2fs" % (time.monotonic() - started))
        phone.accept(ready)
        print("transcript hash matches; session", ready["session_id"])

        await send(phone.seal({"action": "pairing_client_finished"}))
        confirmed = phone.open(json.loads(await reply()))
        token = confirmed.pop("sdk_token", None)
        print("confirmed:", confirmed, "| sdk_token present:", bool(token))

        await send(phone.seal({"action": "wifi_scan"}))
        scan = phone.open(json.loads(await reply(40)))
        print("wifi_scan_result: %d networks, strongest rssi %s" % (
            len(scan["networks"]), scan["networks"][0]["rssi"] if scan["networks"] else None))

        if provision:
            await send(phone.seal({
                "action": "provision_v2", "ssid": provision[0], "password": provision[1],
                "access_token": "not-a-real-token", "refresh_token": "not-a-real-token",
                "token_type": "device", "username": "phone-sim", "api_url": "",
                "api_url_v2": "", "noise_host": "hatch.metaaivm.com"}))
            while True:
                status = phone.open(json.loads(await reply(60)))["status"]
                print("status:", status)
                if status in ("auth_ok", "auth_failed", "wifi_failed", "error_storage"):
                    break
    print("PASS")


asyncio.run(main())
