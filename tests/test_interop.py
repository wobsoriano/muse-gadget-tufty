"""The port against Meta's reference client, run under CPython.

    uv run --with pytest --with cryptography --with websockets pytest tests/test_interop.py

The reference `musegadget` package plays the other side of every exchange:
its Noise responder and envelope codec stand in for the VM, and its
PairingSession is compared field by field with ours.
"""

import asyncio
import base64
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "muse-gadget-sdk" / "linux"
sys.path.insert(0, str(ROOT / "app" / "muse"))
sys.path.insert(0, str(VENDOR / "src"))

from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

import musegadget.link_client as ref_link  # noqa: E402
import musegadget.pairing as ref_pairing  # noqa: E402
from musegadget.noise import (  # noqa: E402
    ApplicationResponse, BodyChunk, NoiseFrameDecoder, NoiseXXResponder, Reset, ResetCode,
    ServiceFrame, encode_noise_frames,
)
from musegadget.noise.transport import decode_request_envelope, encode_response_envelope  # noqa: E402

from musebadge import link, mcrypto, noise, pairing  # noqa: E402
from musebadge.compat import b64url_decode, b64url_encode  # noqa: E402

VECTORS = json.loads((VENDOR / "tests" / "vectors" / "link_pairing_v5.json").read_text())["vectors"]
APP = next(v for v in VECTORS if v["name"] == "community_app_v5")


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# -- Noise ----------------------------------------------------------------------


def handshake():
    initiator = noise.NoiseXXInitiator()
    responder = NoiseXXResponder()
    responder.initialize()
    msg2 = responder.read_message1_and_write_message2(initiator.write_message1())
    initiator.read_message2(msg2)
    responder.read_message3(initiator.write_message3())
    send, recv = initiator.split()
    vm_send, vm_recv = responder.split()
    return noise.NoiseTransport(send, recv), vm_send, vm_recv


def vm_open(vm_recv, decoder, frames):
    for frame in frames:
        assembled = decoder.decode(vm_recv.decrypt_with_ad(b"", frame))
        if assembled is not None:
            return decode_request_envelope(assembled)
    raise AssertionError("no complete frame")


def vm_seal(vm_send, frame):
    return [vm_send.encrypt_with_ad(b"", c)
            for c in encode_noise_frames(encode_response_envelope(frame))]


def test_noise_request_and_body_chunks_reach_the_reference_vm():
    transport, vm_send, vm_recv = handshake()
    decoder = NoiseFrameDecoder()

    stream_id, frames = transport.request(
        "POST", "/chat/stream", b'{"m":1}', (("Content-Type", "application/json"), ("x-app-id", "a")))
    request = vm_open(vm_recv, decoder, frames)
    assert (request.kind, request.stream_id) == ("request", stream_id)
    assert (request.value.verb, request.value.path, request.value.body, request.value.end_body) == (
        "POST", "/chat/stream", b'{"m":1}', True)
    assert [(h.key, h.value) for h in request.value.headers] == [
        ("Content-Type", "application/json"), ("x-app-id", "a")]

    stream_id2, frames = transport.request("POST", "/link-control", end_body=False)
    request = vm_open(vm_recv, decoder, frames)
    assert stream_id2 == stream_id + 1 and request.value.end_body is False

    chunk = vm_open(vm_recv, decoder, transport.body_chunk(stream_id2, b"hello"))
    assert (chunk.kind, chunk.value.data, chunk.value.end_body) == ("body_chunk", b"hello", False)

    reset = vm_open(vm_recv, decoder, transport.reset(stream_id2, "bye"))
    assert (reset.kind, reset.value.reason, reset.value.code) == ("reset", "bye", ResetCode.CANCELLED)


def test_noise_large_body_is_chunked_like_the_reference():
    transport, vm_send, vm_recv = handshake()
    big = bytes(range(256)) * 600  # 153600 bytes, three transport chunks
    frames = transport.body_chunk(7, big)
    assert len(frames) == 3
    chunk = vm_open(vm_recv, NoiseFrameDecoder(), frames)
    assert chunk.value.data == big

    replies = vm_seal(vm_send, ServiceFrame.body_chunk(7, BodyChunk(data=big, end_body=True)))
    assert len(replies) == 3
    decoded = [transport.decrypt_frame(r) for r in replies]
    assert decoded[:2] == [None, None]
    assert (decoded[2].kind, decoded[2].stream_id, decoded[2].data, decoded[2].end_body) == (
        "body_chunk", 7, big, True)


def test_noise_decodes_reference_responses():
    transport, vm_send, vm_recv = handshake()
    (sealed,) = vm_seal(vm_send, ServiceFrame.response(
        3, ApplicationResponse(status=403, body=b"no", end_body=True)))
    frame = transport.decrypt_frame(sealed)
    assert (frame.kind, frame.stream_id, frame.status, frame.data, frame.end_body) == (
        "response", 3, 403, b"no", True)

    (sealed,) = vm_seal(vm_send, ServiceFrame.reset(
        3, Reset(code=ResetCode.TIMEOUT, reason="slow")))
    frame = transport.decrypt_frame(sealed)
    assert (frame.kind, frame.reason) == ("reset", "slow")


def test_noise_rejects_tampered_and_low_order_input():
    transport, vm_send, vm_recv = handshake()
    (sealed,) = vm_seal(vm_send, ServiceFrame.body_chunk(1, BodyChunk(data=b"x")))
    with pytest.raises(noise.NoiseError):
        transport.decrypt_frame(sealed[:-1] + bytes([sealed[-1] ^ 1]))

    initiator = noise.NoiseXXInitiator()
    initiator.write_message1()
    with pytest.raises(noise.NoiseError):
        initiator.read_message2(bytes(32) + bytes(64))


# -- Link session ---------------------------------------------------------------


class Pipe:
    def __init__(self, inbox, outbox):
        self._inbox, self._outbox = inbox, outbox

    async def send(self, data):
        await self._outbox.put(data)

    async def recv(self):
        data = await self._inbox.get()
        if data is None:
            raise ConnectionError("closed")
        return data

    async def close(self):
        await self._outbox.put(None)
        await self._inbox.put(None)


class FakeVm:
    """The reference test's VM: Noise responder plus the control stream."""

    def __init__(self, ws):
        self.ws = ws
        self.decoder = NoiseFrameDecoder()
        self.messages = ref_link.MessageDecoder()
        self.stream_id = 0

    async def handshake(self):
        responder = NoiseXXResponder()
        responder.initialize()
        await self.ws.send(responder.read_message1_and_write_message2(await self.ws.recv()))
        responder.read_message3(await self.ws.recv())
        self.send_cipher, self.recv_cipher = responder.split()

    async def next_frame(self):
        while True:
            plain = self.recv_cipher.decrypt_with_ad(b"", await self.ws.recv())
            assembled = self.decoder.decode(plain)
            if assembled is not None:
                return decode_request_envelope(assembled)

    async def next_message(self):
        while True:
            frame = await self.next_frame()
            assert frame.kind == "body_chunk"
            messages = self.messages.feed(frame.value.data)
            if messages:
                return messages[0]

    async def send_frame(self, frame):
        for chunk in encode_noise_frames(encode_response_envelope(frame)):
            await self.ws.send(self.send_cipher.encrypt_with_ad(b"", chunk))

    async def accept_control_stream(self, status=200):
        request = await self.next_frame()
        self.stream_id = request.stream_id
        await self.send_frame(ServiceFrame.response(
            self.stream_id, ApplicationResponse(status=status, end_body=status >= 400)))
        return request

    async def send_message(self, message):
        await self.send_frame(ServiceFrame.body_chunk(
            self.stream_id, BodyChunk(data=ref_link.encode_message(message))))


REGISTER = {"node_id": "homelink-abcdef", "platform": "linux", "device_family": "homehub",
            "commands_v2": {"badge.show_message": {"description": "d", "required": {}, "optional": {}}}}


session_ws = []


def make_session(run_command, connects):
    to_device, to_vm = asyncio.Queue(), asyncio.Queue()
    device_ws, vm_ws = Pipe(to_device, to_vm), Pipe(to_vm, to_device)
    session_ws[:] = [device_ws]

    async def connect(url, headers):
        connects.append((url, headers))
        return device_ws

    session = link.LinkSession(
        "gw.example", "vm 1&x", "tok", REGISTER, run_command, connect, log=lambda m: None)
    return session, FakeVm(vm_ws)


def test_register_invoke_result_chat_and_unpair():
    async def scenario():
        calls, connects = [], []

        async def run_command(command, params, timeout_ms):
            calls.append((command, params, timeout_ms))
            return {"ok": True, "payload": {"shown": True}}

        session, vm = make_session(run_command, connects)
        task = asyncio.ensure_future(session.run())
        await vm.handshake()
        request = await vm.accept_control_stream()
        assert (request.kind, request.value.verb, request.value.path, request.value.end_body) == (
            "request", "POST", "/link-control", False)

        register = await vm.next_message()
        assert (register["type"], register["method"]) == ("req", "link.register")
        assert register["params"] == REGISTER
        await vm.send_message({"type": "res", "id": register["id"], "ok": True})

        await vm.send_message({
            "method": "link.invoke", "id": "inv-1", "command": "badge.show_message",
            "params": {"text": "hi"}, "timeout_ms": 5000,
        })
        assert await vm.next_message() == {
            "method": "link.result", "id": "inv-1", "ok": True, "payload": {"shown": True}}
        assert calls == [("badge.show_message", {"text": "hi"}, 5000)]
        assert session.connected

        chat = asyncio.ensure_future(session.send_chat("button A pressed", session_id="side-1"))
        chat_request = await vm.next_frame()
        assert (chat_request.value.verb, chat_request.value.path, chat_request.value.end_body) == (
            "POST", "/chat/stream", True)
        assert json.loads(chat_request.value.body) == {
            "message": "button A pressed", "output_modality": "text",
            "device_id": "homelink-abcdef", "session_id": "side-1"}
        headers = {h.key: h.value for h in chat_request.value.headers}
        assert headers["Content-Type"] == "application/json" and headers["x-app-id"] == "musegadget"
        await vm.send_frame(ServiceFrame.response(chat_request.stream_id, ApplicationResponse(
            status=200, body=b'{"message_id":"m1"}', end_body=True)))
        assert await asyncio.wait_for(chat, 2) == {
            "ok": True, "status": 200, "response": {"message_id": "m1"}}

        await vm.send_message({"type": "evt", "event": "link.unpaired"})
        assert await asyncio.wait_for(task, 2) == link.UNPAIRED
        assert connects == [("wss://gw.example/v1/noise?vm_id=vm%201%26x",
                             {"Authorization": "Bearer tok"})]

    asyncio.run(scenario())


def test_failing_command_reports_an_error_result():
    async def scenario():
        async def run_command(command, params, timeout_ms):
            raise RuntimeError("boom")

        session, vm = make_session(run_command, [])
        task = asyncio.ensure_future(session.run())
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        await vm.send_message({"method": "link.invoke", "id": "i", "command": "x"})
        assert await vm.next_message() == {
            "method": "link.result", "id": "i", "ok": False, "error": "RuntimeError: boom"}
        await session.stop()
        assert await asyncio.wait_for(task, 2) == link.CLOSED

    asyncio.run(scenario())


def test_dead_connection_ends_the_session(monkeypatch):
    # A Wi-Fi drop leaves the socket open but silent: reads never return and
    # writes block. The session must still end so the badge reconnects.
    monkeypatch.setattr(link, "PING_INTERVAL_S", 0.05)
    monkeypatch.setattr(link, "PING_TIMEOUT_S", 0.05)

    async def scenario():
        async def run_command(*args):
            return {"ok": True}

        session, vm = make_session(run_command, [])
        ws = session_ws[0]
        ws.last_rx = link.monotonic()

        async def ping_that_never_returns():
            await asyncio.sleep(3600)

        async def close_that_never_returns():
            await asyncio.sleep(3600)

        ws.ping = ping_that_never_returns
        monkeypatch.setattr(link, "CLOSE_TIMEOUT_S", 0.05)
        ws.close = close_that_never_returns
        task = asyncio.ensure_future(session.run())
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        assert await asyncio.wait_for(task, 2) == link.CLOSED

    asyncio.run(scenario())


def test_forbidden_control_stream():
    async def scenario():
        async def run_command(*args):
            return {"ok": True}

        session, vm = make_session(run_command, [])
        task = asyncio.ensure_future(session.run())
        await vm.handshake()
        await vm.accept_control_stream(status=403)
        assert await asyncio.wait_for(task, 2) == link.FORBIDDEN

    asyncio.run(scenario())


def test_message_decoder_matches_the_reference():
    data = link.encode_message({"a": 1}) + ref_link.encode_message({"b": 2}) + bytes(4)
    assert link.encode_message({"a": 1, "b": [1, "x"]}) == ref_link.encode_message({"a": 1, "b": [1, "x"]})
    decoder = link.MessageDecoder()
    assert decoder.feed(data[:5]) == []
    assert decoder.feed(data[5:]) == [{"a": 1}, {"b": 2}]
    with pytest.raises(ValueError):
        link.MessageDecoder().feed(b"\x00\x00\x00\x40")


# -- Pairing --------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Mobile:
    """Phone side of the record layer, from the reference tests."""

    def __init__(self, tx_key, rx_key, session_id):
        self.tx, self.rx = AESGCM(tx_key), AESGCM(rx_key)
        self.session_id = session_id
        self.tx_counter = 0

    def seal(self, command):
        counter = self.tx_counter
        self.tx_counter += 1
        sealed = self.tx.encrypt(
            bytes([0, 0, 0, 0]) + counter.to_bytes(8, "big"), json.dumps(command).encode(),
            f"hatch-link ble setup v1|{self.session_id}|m2d|{counter}".encode())
        return {"action": "pairing_encrypted", "session_id": self.session_id,
                "counter": str(counter), "ciphertext": b64url_encode(sealed[:-16]),
                "tag": b64url_encode(sealed[-16:])}

    def open(self, envelope):
        counter = int(envelope["counter"])
        return json.loads(self.rx.decrypt(
            bytes([1, 0, 0, 0]) + counter.to_bytes(8, "big"),
            unb64(envelope["ciphertext"]) + unb64(envelope["tag"]),
            f"hatch-link ble setup v1|{self.session_id}|d2m|{counter}".encode()))


def hello(vector=APP):
    return {"action": "pairing_client_hello", "version": 5, "pairing_auth": "none",
            "pairing_policy": "confirm_app", "mobile_pub": vector["mobile_pub"],
            "mobile_nonce": vector["mobile_nonce"]}


def devices(clock=None, sdk_token=None):
    """Our session and the reference one, fed the vector's key and nonce."""
    scalar = int(APP["device_private_scalar_hex"], 16)
    common = dict(node_id=APP["node_id"], device_id=APP["device_id"], mac=APP["mac"],
                  firmware_version=APP["firmware_version"], sdk_token=sdk_token)
    ours = pairing.PairingSession(
        clock=clock or FakeClock(),
        generate_key=lambda: (scalar, mcrypto.p256_public(scalar)),
        random_bytes=lambda n: unb64(APP["device_nonce"]), **common)
    reference = ref_pairing.PairingSession(
        clock=FakeClock(),
        generate_key=lambda: ec.derive_private_key(scalar, ec.SECP256R1()),
        random_bytes=lambda n: unb64(APP["device_nonce"]), **common)
    return ours, reference


def test_pairing_ready_matches_reference_and_vector():
    ours, reference = devices()
    ready = ours.handle_hello(hello())
    assert ready == reference.handle_hello(hello())
    assert ready["device_pub"] == APP["device_pub"]
    assert ready["transcript_hash"] == APP["transcript_hash"]
    assert ready["session_id"] == APP["session_id"]
    assert ours.device_info() == reference.device_info()


def test_pairing_records_match_reference_byte_for_byte():
    ours, reference = devices(sdk_token="mgst_test")
    ours.handle_hello(hello())
    reference.handle_hello(hello())
    finished = {"action": "pairing_encrypted", "session_id": APP["session_id"], "counter": "0",
                "ciphertext": APP["client_finished_ciphertext"], "tag": APP["client_finished_tag"]}
    command = json.loads(ours.decrypt(finished))
    assert command == json.loads(reference.decrypt(finished)) == {"action": "pairing_client_finished"}
    generation = ours.handle_client_finished(command)
    assert generation and generation == reference.handle_client_finished(command)
    assert ours.confirmed

    # Same keys and counters, so the sealed records must be identical.
    assert ours.encrypt_status("pairing_confirmed", generation) == \
        reference.encrypt_status("pairing_confirmed", generation)
    assert ours.encrypt_json('{"type":"wifi_scan_result","networks":[]}') == \
        reference.encrypt_json('{"type":"wifi_scan_result","networks":[]}')

    mobile = Mobile(bytes.fromhex(APP["mobile_tx_key_hex"]),
                    bytes.fromhex(APP["mobile_rx_key_hex"]), APP["session_id"])
    mobile.tx_counter = 1
    assert json.loads(ours.decrypt(mobile.seal({"action": "wifi_scan"}))) == {"action": "wifi_scan"}
    assert mobile.open(ours.encrypt_status("auth_ok")) == {"type": "status", "status": "auth_ok"}

    provisioning = ours.mark_provisioning()
    assert provisioning == reference.mark_provisioning()
    assert ours.provisioning_valid(provisioning) and not ours.provisioning_valid(generation)
    assert ours.encrypt_status("wifi_connecting", generation) is None


@pytest.mark.parametrize("change", [
    {"version": 4}, {"version": True}, {"pairing_auth": "fleet_ecdsa_p256_v1"},
    {"pairing_policy": "confirm_press"}, {"mobile_pub": "AAAA"}, {"mobile_nonce": "AAAA"},
    {"mobile_pub": b64url_encode(b"\x04" + bytes(64))}, {"mobile_pub": 7},
])
def test_pairing_rejects_bad_hello_like_the_reference(change):
    ours, reference = devices()
    message = dict(hello(), **change)
    with pytest.raises(ref_pairing.PairingError) as ref_error:
        reference.handle_hello(message)
    with pytest.raises(pairing.PairingError) as our_error:
        ours.handle_hello(message)
    assert our_error.value.status == ref_error.value.status == pairing.ERROR_INVALID_HELLO
    assert ours.state == pairing.IDLE


def test_pairing_replay_bad_tag_and_expiry_clear_the_session():
    finished = {"action": "pairing_encrypted", "session_id": APP["session_id"], "counter": "0",
                "ciphertext": APP["client_finished_ciphertext"], "tag": APP["client_finished_tag"]}

    ours, _ = devices()
    ours.handle_hello(hello())
    ours.decrypt(finished)
    with pytest.raises(pairing.PairingError):
        ours.decrypt(finished)  # replayed counter
    assert ours.state == pairing.IDLE

    ours, _ = devices()
    ours.handle_hello(hello())
    with pytest.raises(pairing.PairingError):
        ours.decrypt(dict(finished, tag=b64url_encode(bytes(16))))
    assert ours.state == pairing.IDLE

    clock = FakeClock()
    ours, _ = devices(clock=clock)
    ours.handle_hello(hello())
    clock.now += pairing.CLIENT_FINISHED_TIMEOUT_S + 1
    with pytest.raises(pairing.PairingError):
        ours.decrypt(finished)

    ours, _ = devices()
    ours.handle_hello(hello())
    assert ours.handle_client_finished({"action": "pairing_client_finished"}) == 0  # before any record


def test_b64url_matches_the_reference():
    for sample in (b"", b"\xff", b"\xfb\xff\xfe", bytes(range(40))):
        if sample:
            assert b64url_encode(sample) == ref_pairing.b64url_encode(sample)
            assert b64url_decode(b64url_encode(sample)) == sample
    for bad in ("", "A", "AAAA=", "AA+A", "AA/A", "AAAAA", 5, None):
        with pytest.raises(ValueError):
            ref_pairing.b64url_decode(bad)
        with pytest.raises(ValueError):
            b64url_decode(bad)
