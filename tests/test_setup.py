"""The BLE setup conversation, driven by a simulated phone.

    uv run --with pytest --with cryptography --with websockets pytest tests/test_setup.py

The phone side is written from the protocol with the `cryptography` package,
so it shares no code with the port.
"""

import asyncio
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "muse"))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand  # noqa: E402

from musebadge import ble_framing, pairing, setup, store  # noqa: E402
from musebadge.compat import b64url_decode, b64url_encode  # noqa: E402


class Phone:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.nonce = os.urandom(16)
        self.pub = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.tx_counter = 0

    def hello(self):
        return {"action": "pairing_client_hello", "version": 5, "pairing_auth": "none",
                "pairing_policy": "confirm_app", "mobile_pub": b64url_encode(self.pub),
                "mobile_nonce": b64url_encode(self.nonce)}

    def accept(self, ready):
        device_pub = b64url_decode(ready["device_pub"])
        transcript = "\n".join([
            "hatch-link-pairing-v5", "version=5", "initiator_role=mobile", "responder_role=link",
            "device_id=" + ready["device_id"], "node_id=" + ready["node_id"], "mac=" + ready["mac"],
            "model=hatch_link", "firmware_version=" + ready["firmware_version"],
            "selected_cipher_suite=p256-hkdf-sha256-aes-gcm-v1", "pairing_auth=none",
            "pairing_auth_epoch=0", "pairing_policy=confirm_app", "confirm_timeout_seconds=0",
            "mobile_pub=" + b64url_encode(self.pub), "device_pub=" + ready["device_pub"],
            "mobile_nonce=" + b64url_encode(self.nonce), "device_nonce=" + ready["device_nonce"],
        ])
        transcript_hash = hashlib.sha256(transcript.encode()).digest()
        assert b64url_encode(transcript_hash) == ready["transcript_hash"]
        secret = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), device_pub))
        salt = hashlib.sha256(self.nonce + b64url_decode(ready["device_nonce"]) + transcript_hash).digest()
        session = HKDF(hashes.SHA256(), 32, salt, b"hatch-link ble setup v1").derive(secret)
        self.tx = AESGCM(HKDFExpand(hashes.SHA256(), 32, b"mobile->device").derive(session))
        self.rx = AESGCM(HKDFExpand(hashes.SHA256(), 32, b"device->mobile").derive(session))
        self.session_id = ready["session_id"]

    def seal(self, command):
        counter = self.tx_counter
        self.tx_counter += 1
        sealed = self.tx.encrypt(
            bytes(4) + counter.to_bytes(8, "big"), json.dumps(command).encode(),
            f"hatch-link ble setup v1|{self.session_id}|m2d|{counter}".encode())
        return {"action": "pairing_encrypted", "session_id": self.session_id,
                "counter": str(counter), "ciphertext": b64url_encode(sealed[:-16]),
                "tag": b64url_encode(sealed[-16:])}

    def open(self, envelope):
        counter = int(envelope["counter"])
        return json.loads(self.rx.decrypt(
            bytes([1, 0, 0, 0]) + counter.to_bytes(8, "big"),
            b64url_decode(envelope["ciphertext"], 16384) + b64url_decode(envelope["tag"]),
            f"hatch-link ble setup v1|{self.session_id}|d2m|{counter}".encode()))


class Transport:
    def __init__(self, mtu=23):
        self._mtu = mtu
        self._assembler = ble_framing.ChunkAssembler()
        self.messages = []
        self.disconnects = []

    def mtu(self):
        return self._mtu

    async def send_packets(self, packets):
        for packet in packets:
            # Plaintext error statuses go out unframed, as in the reference.
            assert packet[0] != ble_framing.CHUNK_MAGIC or len(packet) <= self._mtu - 3
            message = self._assembler.feed(packet)
            if message is not None:
                self.messages.append(message)

    def disconnect(self, delay):
        self.disconnects.append(delay)


class Network:
    def __init__(self, online, joinable=True):
        self.online = online
        self.joinable = joinable
        self.joined = None

    def is_online(self):
        return self.online

    def current_connection_entry(self):
        return {"ssid": "HomeNet", "rssi": -40, "secure": False}

    async def scan(self):
        return [{"ssid": "HomeNet", "rssi": -50, "secure": True},
                {"ssid": "Hotspot", "rssi": -60, "secure": True}]

    async def join(self, ssid, password):
        self.joined = (ssid, password)
        self.online = self.joinable
        return self.joinable


def build(tmp_path, network, verify_ok=True, mtu=23):
    files = store.Store(str(tmp_path))
    identity = store.load_identity(files)
    session = pairing.PairingSession(
        identity.node_id, identity.device_id, identity.mac, "0.1.0", sdk_token="mgst_x")
    transport = Transport(mtu)
    completed = []

    async def provision(credentials):
        if not verify_ok:
            raise setup.ProvisionFailed("auth_failed")
        return dict(credentials, token_type="device")

    controller = setup.SetupController(
        session, identity, "0.1.0", transport, network, provision,
        lambda record: files.save(store.PAIRING_FILE, record),
        on_complete=lambda: completed.append(True), log=lambda m: None)
    return controller, transport, files, identity, completed


async def send(controller, transport, command, mtu=23):
    for packet in ble_framing.encode_chunks(json.dumps(command).encode(), mtu):
        controller.on_write(packet)
    await settle(transport, len(transport.messages) + 1)


async def settle(transport, count):
    for _ in range(200):
        if len(transport.messages) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected %d messages, got %r" % (count, transport.messages))


PROVISION = {"action": "provision_v2", "ssid": "HomeNet", "password": "pw",
             "access_token": "acc", "refresh_token": "ref", "token_type": "device",
             "username": "rob", "api_url": "https://a", "api_url_v2": "https://b",
             "noise_host": "hatch.metaaivm.com"}


def run_flow(tmp_path, network, verify_ok=True):
    async def scenario():
        controller, transport, files, identity, completed = build(tmp_path, network, verify_ok)
        task = asyncio.ensure_future(controller.run())
        phone = Phone()
        seen = []

        async def step(command, replies=1):
            start = len(transport.messages)
            for packet in ble_framing.encode_chunks(json.dumps(command).encode(), 23):
                controller.on_write(packet)
            await settle(transport, start + replies)
            return transport.messages[start:]

        (info,) = await step({"action": "get_device_info"})
        info = json.loads(info)
        assert info["type"] == "device_info" and info["node_id"] == identity.node_id
        assert info["model"] == "hatch_link" and info["pairing_policy"] == "confirm_app"
        assert info["network_ready"] == network.online

        (refused,) = await step({"action": "wifi_scan"})
        assert refused == b"error_encryption_required"

        (ready,) = await step(phone.hello())
        phone.accept(json.loads(ready))

        (confirmed,) = await step(phone.seal({"action": "pairing_client_finished"}))
        assert phone.open(json.loads(confirmed)) == {
            "type": "status", "status": "pairing_confirmed", "sdk_token": "mgst_x"}

        (scan,) = await step(phone.seal({"action": "wifi_scan"}))
        seen.append(phone.open(json.loads(scan)))

        for raw in await step(phone.seal(PROVISION), replies=3):
            seen.append(phone.open(json.loads(raw)))
        task.cancel()
        return seen, files.load(store.PAIRING_FILE), completed, transport.disconnects

    return asyncio.run(scenario())


def test_setup_when_already_online(tmp_path):
    network = Network(online=True)
    seen, saved, completed, disconnects = run_flow(tmp_path, network)
    assert seen == [
        {"type": "wifi_scan_result", "networks": [
            {"ssid": "HomeNet", "rssi": -40, "secure": False},
            {"ssid": "Hotspot", "rssi": -60, "secure": True}]},
        {"type": "status", "status": "wifi_connecting"},
        {"type": "status", "status": "wifi_connected"},
        {"type": "status", "status": "auth_ok"},
    ]
    assert network.joined is None
    assert saved["access_token"] == "acc" and saved["noise_host"] == "hatch.metaaivm.com"
    assert completed == [True] and disconnects == []


def test_setup_joins_wifi_when_offline(tmp_path):
    network = Network(online=False)
    seen, saved, completed, _ = run_flow(tmp_path, network)
    assert seen[0] == {"type": "wifi_scan_result", "networks": [
        {"ssid": "HomeNet", "rssi": -50, "secure": True},
        {"ssid": "Hotspot", "rssi": -60, "secure": True}]}
    assert [m["status"] for m in seen[1:]] == ["wifi_connecting", "wifi_connected", "auth_ok"]
    assert network.joined == ("HomeNet", "pw")
    assert saved is not None and completed == [True]


def test_setup_switches_network_when_the_app_picks_another(tmp_path, monkeypatch):
    # Joined to a portal network that blocks the internet: the app must be
    # able to move the badge to a different one.
    monkeypatch.setitem(PROVISION, "ssid", "Hotspot")
    network = Network(online=True)
    seen, saved, completed, _ = run_flow(tmp_path, network)
    assert network.joined == ("Hotspot", "pw")
    assert [m["status"] for m in seen[1:]] == ["wifi_connecting", "wifi_connected", "auth_ok"]


def test_rejected_token_is_not_saved(tmp_path):
    seen, saved, completed, disconnects = run_flow(tmp_path, Network(online=True), verify_ok=False)
    assert [m["status"] for m in seen[1:]] == ["wifi_connecting", "wifi_connected", "auth_failed"]
    assert saved is None and completed == [] and disconnects == [0.5]


def test_wifi_failure_keeps_the_session_for_a_retry(tmp_path):
    async def scenario():
        network = Network(online=False, joinable=False)
        controller, transport, files, identity, completed = build(tmp_path, network)
        task = asyncio.ensure_future(controller.run())
        phone = Phone()
        await send(controller, transport, phone.hello())
        phone.accept(json.loads(transport.messages[-1]))
        await send(controller, transport, phone.seal({"action": "pairing_client_finished"}))
        start = len(transport.messages)
        for packet in ble_framing.encode_chunks(json.dumps(phone.seal(PROVISION)).encode(), 23):
            controller.on_write(packet)
        await settle(transport, start + 2)
        statuses = [phone.open(json.loads(m))["status"] for m in transport.messages[start:]]
        assert statuses == ["wifi_connecting", "wifi_failed"]
        assert files.load(store.PAIRING_FILE) is None
        task.cancel()

    asyncio.run(scenario())


def test_tampered_record_ends_the_session(tmp_path):
    async def scenario():
        controller, transport, files, identity, completed = build(tmp_path, Network(online=True))
        task = asyncio.ensure_future(controller.run())
        phone = Phone()
        await send(controller, transport, phone.hello())
        phone.accept(json.loads(transport.messages[-1]))
        record = phone.seal({"action": "pairing_client_finished"})
        record["tag"] = b64url_encode(bytes(16))
        before = len(transport.messages)
        for packet in ble_framing.encode_chunks(json.dumps(record).encode(), 23):
            controller.on_write(packet)
        for _ in range(100):
            if transport.disconnects:
                break
            await asyncio.sleep(0.01)
        # The session is gone and plaintext is blocked, so the error status is
        # suppressed and the phone only sees the disconnect, as in the reference.
        assert len(transport.messages) == before
        assert transport.disconnects == [setup.DISCONNECT_AFTER_ERROR_S]
        task.cancel()

    asyncio.run(scenario())
