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
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/link_client.py)
# to MicroPython for the Pimoroni Tufty 2350.

"""One control session with a Muse VM.

Ported from the Linux Device SDK's `link_client.py`. Opens a WebSocket to
`/v1/noise` with the per-VM bearer, runs the Noise XX handshake, then opens a
long-lived `POST /link-control` stream. Both directions of that stream carry
JSON messages, each prefixed with its length as a little-endian u32:

* device to VM: `link.register`, then a `link.result` for each invoke
* VM to device: the register reply, `link.invoke`, and events such as
  `link.unpaired`

Messages the device sends to the Muse (`send_chat`) go as separate
`POST /chat/stream` requests on the same session.
"""

import asyncio
import json
import struct

from .compat import compact_json, monotonic, uuid4
from .net import UpgradeRejected
from .noise import NoiseTransport, NoiseXXInitiator, generate_keypairs

NOISE_PATH = "/v1/noise"
CONTROL_PATH = "/link-control"
CHAT_PATH = "/chat/stream"
SUBSCRIBE_PATH = "/chat/subscribe"
APP_ID = "musegadget"
REQUEST_TIMEOUT_S = 60
CLOSE_TIMEOUT_S = 5
FIRST_REPLY_S = 300
REPLY_QUIET_S = 3
TURN_CAP_S = 900
TURN_POLL_S = 0.25
MAX_EVENT_LINE = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
HANDSHAKE_TIMEOUT_S = 20
PING_INTERVAL_S = 15
PING_TIMEOUT_S = 10
RX_TIMEOUT_S = 45
MAX_INBOUND_MESSAGE = 4 * 1024 * 1024

CLOSED = "closed"                # connection ended; reconnect normally
AUTH_REJECTED = "auth_rejected"  # edge refused the VM bearer; re-fetch VMs
FORBIDDEN = "forbidden"          # authenticated but not allowed right now
UNPAIRED = "unpaired"            # the Muse removed this device

# Matches JavaScript's encodeURIComponent, as the firmware does.
_URI_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()"


def encode_message(obj):
    data = compact_json(obj).encode()
    return struct.pack("<I", len(data)) + data


class MessageDecoder:
    """Splits the control stream into length-prefixed JSON messages."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data
        messages = []
        while len(self._buf) >= 4:
            length = struct.unpack_from("<I", self._buf)[0]
            if length > MAX_INBOUND_MESSAGE:
                raise ValueError("inbound message too large")
            if len(self._buf) < 4 + length:
                break
            raw = bytes(self._buf[4:4 + length])
            self._buf = self._buf[4 + length:]
            if not raw:
                continue  # keepalive
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages


def noise_url(noise_host, vm_id):
    quoted = "".join(
        c if c in _URI_SAFE else "".join("%%%02X" % b for b in c.encode())
        for c in vm_id
    )
    return "wss://%s%s?vm_id=%s" % (noise_host, NOISE_PATH, quoted)


class _Request:
    """Collects the response to one request stream."""

    def __init__(self):
        self.done = asyncio.Event()
        self.status = 0
        self.body = bytearray()
        self.error = None

    def on_frame(self, frame):
        if self.done.is_set():
            return
        if frame.kind == "reset":
            self.error = "stream reset: " + frame.reason
            self.done.set()
            return
        if frame.kind == "response":
            self.status = frame.status
        self.body += frame.data
        if len(self.body) > MAX_RESPONSE_BYTES:
            self.error = "response too large"
            self.done.set()
        elif frame.end_body:
            self.done.set()


class _Subscription:
    """The chat event stream: newline-delimited JSON, one event per line."""

    def __init__(self, on_event):
        self._on_event = on_event
        self._buf = bytearray()
        self.closed = False
        self.error = ""

    def on_frame(self, frame):
        if frame.kind == "reset":
            self.closed, self.error = True, frame.reason
            return
        if frame.kind == "response" and frame.status >= 400:
            self.closed, self.error = True, "HTTP %d" % frame.status
            return
        self._buf += frame.data
        while True:
            end = self._buf.find(b"\n")
            if end < 0:
                break
            line = bytes(self._buf[:end])
            self._buf = self._buf[end + 1:]
            try:
                event = json.loads(line) if line.strip() else None
            except ValueError:
                event = None
            # Other line types are the subscribe ack.
            if isinstance(event, dict) and event.get("type") == "event":
                self._on_event(event)
        if len(self._buf) > MAX_EVENT_LINE:
            self.closed, self.error = True, "event line too long"
        if frame.end_body:
            self.closed, self.error = True, self.error or "ended by the Muse"


class _Turn:
    """The events of one question, and the reply they add up to."""

    _KEPT = ("message.user", "delta.message_start", "delta.text_append",
             "delta.message_done", "message.assistant")

    def __init__(self):
        self.busy = False
        self.last_event = monotonic()
        self._message_id = None
        self._acked_at = None
        self._events = []

    @property
    def message_id(self):
        return self._message_id

    @message_id.setter
    def message_id(self, value):
        self._message_id = value
        self._acked_at = len(self._events)

    def add(self, event):
        self.last_event = monotonic()
        name = event.get("event")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = event["payload"] = {}
        if name in ("agent.status", "task.status"):
            activity = payload.get("activity_code")
            if activity:
                self.busy = activity not in ("online", "idle")
            elif payload.get("status"):
                self.busy = payload["status"] not in ("completed", "failed")
        elif name in self._KEPT:
            self._events.append(event)

    def reply(self):
        """(reply text so far, whether every reply message is complete).

        The Muse does not link its reply to the question: `reply_to_message_id`
        comes back empty. So, as the reference firmware does, a message that
        starts after ours belongs to the turn. "After ours" is after the
        stream echoes our message, or after the ack if the echo came first.
        Events are kept and matched here because they can arrive before the
        ack says what our message id is.
        """
        if not self._message_id:
            return "", False
        order, messages = [], {}
        ours_seen = False
        for index, event in enumerate(self._events):
            name = event["event"]
            payload = event["payload"]
            message_id = payload.get("message_id") or event.get("message_id") or payload.get("id")
            if name == "message.user":
                ours_seen = ours_seen or message_id == self._message_id
                continue
            if message_id not in messages:
                if name == "delta.text_append":
                    continue
                parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
                linked = parent == self._message_id or parent in messages
                follows = not parent and (ours_seen or index >= self._acked_at)
                if not (linked or follows):
                    continue
                messages[message_id] = ["", False]
                order.append(message_id)
            entry = messages[message_id]
            if name == "delta.text_append":
                entry[0] += payload.get("text") or ""
            elif name != "delta.message_start":
                final = payload.get("display_text") or payload.get("content")
                if isinstance(final, str) and final:
                    entry[0] = final
                if name == "delta.message_done" or payload.get("display_text_ready") is not False:
                    entry[1] = True
        texts = [messages[m][0] for m in order if messages[m][0]]
        return "\n\n".join(texts), bool(order) and all(messages[m][1] for m in order)


class LinkSession:
    """`connect(url, headers)` opens the WebSocket. `run_command(command,
    params, timeout_ms)` is a coroutine returning the `link.result` fields.
    """

    def __init__(self, noise_host, vm_id, vm_auth_token, register_params,
                 run_command, connect, log=print):
        self._url = noise_url(noise_host, vm_id)
        self._token = vm_auth_token
        self._register_params = register_params
        self._run_command = run_command
        self._connect = connect
        self._log = log
        self._ws = None
        self._transport = None
        # Frames carry consecutive nonces, so sealing and writing one message
        # must not interleave with another.
        self._send_lock = asyncio.Lock()
        self._tasks = []
        self._stream_id = 0
        self._register_id = ""
        self._requests = {}
        self._subscription = None
        self._turn = None
        self._ended = asyncio.Event()
        self._outcome = CLOSED
        self.registered_at = None

    async def run(self):
        """Serve until the connection ends; returns one of the outcomes."""
        # Made before connecting: they are slow on a microcontroller and the
        # server's handshake clock starts at the upgrade.
        keypairs = generate_keypairs()
        try:
            ws = await self._connect(self._url, {"Authorization": "Bearer " + self._token})
        except UpgradeRejected as rejected:
            self._log("VM refused connection: HTTP %d" % rejected.status)
            return AUTH_REJECTED if rejected.status == 401 else FORBIDDEN
        self._ws = ws
        try:
            self._transport = await asyncio.wait_for(
                self._handshake(ws, keypairs), HANDSHAKE_TIMEOUT_S)
            await self._open_control_stream()
            # The reader is a task so the keepalive can end the session even
            # when the socket is dead: a read on a dead connection never
            # returns, and a write to one can block for good.
            self._tasks.append(asyncio.create_task(self._read_until_ended()))
            if hasattr(ws, "ping"):
                self._tasks.append(asyncio.create_task(self._keepalive(ws)))
            await self._ended.wait()
            return self._outcome
        finally:
            for task in self._tasks:
                task.cancel()
            for request in self._requests.values():
                if isinstance(request, _Subscription):
                    request.closed, request.error = True, "session ended"
                else:
                    request.error = request.error or "session ended"
                    request.done.set()
            self._requests.clear()
            self._transport = None
            try:
                await asyncio.wait_for(ws.close(), CLOSE_TIMEOUT_S)
            except Exception:
                pass

    def _end(self, outcome):
        if not self._ended.is_set():
            self._outcome = outcome
            self._ended.set()

    async def _read_until_ended(self):
        self._end(await self._read_loop())

    async def stop(self):
        self._end(CLOSED)

    @property
    def connected(self):
        return self._transport is not None and self.registered_at is not None

    async def _handshake(self, ws, keypairs):
        initiator = NoiseXXInitiator(keypairs)
        await ws.send(initiator.write_message1())
        msg2 = await ws.recv()
        if isinstance(msg2, str):
            raise OSError("Noise handshake got a text frame")
        initiator.read_message2(msg2)
        # The bearer already authenticated us at the upgrade; message 3
        # carries an empty payload.
        await ws.send(initiator.write_message3())
        send, recv = initiator.split()
        self._log("Noise session established")
        return NoiseTransport(send, recv)

    async def _open_control_stream(self):
        async with self._send_lock:
            self._stream_id, frames = self._transport.request(
                "POST", CONTROL_PATH, end_body=False)
            await self._send_frames(frames)
        self._register_id = uuid4()
        await self.send({
            "type": "req",
            "id": self._register_id,
            "method": "link.register",
            "params": self._register_params,
        })
        self._log("sent link.register as %s" % self._register_params.get("node_id"))

    async def _keepalive(self, ws):
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if monotonic() - ws.last_rx <= RX_TIMEOUT_S:
                try:
                    await asyncio.wait_for(ws.ping(), PING_TIMEOUT_S)
                    continue
                except Exception:
                    pass
            self._log("the connection to the Muse went quiet; reconnecting")
            self._end(CLOSED)
            return

    async def request(self, verb, path, body=b"", headers=()):
        """One request on its own stream of this session; returns (status, body)."""
        if self._transport is None:
            raise OSError("not connected")
        request = _Request()
        stream_id = 0
        try:
            async with self._send_lock:
                stream_id, frames = self._transport.request(verb, path, body, headers)
                self._requests[stream_id] = request
                await self._send_frames(frames)
            await asyncio.wait_for(request.done.wait(), REQUEST_TIMEOUT_S)
        finally:
            self._requests.pop(stream_id, None)
        if request.error:
            raise OSError(request.error)
        return request.status, bytes(request.body)

    async def _json(self, verb, path, body=None):
        headers = [("x-request-id", uuid4()), ("x-app-id", APP_ID)]
        if body is not None:
            headers.append(("Content-Type", "application/json"))
        status, raw = await self.request(
            verb, path, json.dumps(body).encode() if body is not None else b"", headers)
        try:
            decoded = json.loads(raw) if raw else None
        except ValueError:
            decoded = raw[:2000].decode()
        return status, decoded

    async def send_chat(self, message, session_id=None):
        """Post a user message to the Muse as coming from this device.

        `session_id` targets a side chat; an id the Muse has not seen before
        starts a new one. Without it the message goes to the main chat. The
        response is only the ack. `ask` also waits for the reply.
        """
        if self._transport is None:
            return {"ok": False, "status": 0, "response": "not connected"}
        body = {
            "message": message,
            "output_modality": "text",
            "device_id": self._register_params.get("node_id"),
        }
        if session_id:
            body["session_id"] = session_id
        status, decoded = await self._json("POST", CHAT_PATH, body)
        return {"ok": 200 <= status < 300, "status": status, "response": decoded}

    async def _subscribe(self):
        """Open the long-lived stream the Muse's chat events arrive on."""
        if self._subscription is not None and not self._subscription.closed:
            return
        subscription = _Subscription(self._on_chat_event)
        async with self._send_lock:
            stream_id, frames = self._transport.request("POST", SUBSCRIBE_PATH, b"{}", (
                ("Content-Type", "application/json"),
                ("accept", "application/x-ndjson"),
                ("x-request-id", uuid4()),
                ("x-app-id", APP_ID),
            ))
            self._requests[stream_id] = subscription
            await self._send_frames(frames)
        self._subscription = subscription

    def _on_chat_event(self, event):
        if self._turn is not None:
            self._turn.add(event)

    async def ask(self, message, first_reply_s=FIRST_REPLY_S, on_text=None):
        """Send a message and return the Muse's reply text.

        The ack carries no reply. The reply streams in on the subscription as
        events, and nothing marks the end of a turn, so it is over once every
        reply message is done and the stream has been quiet for a moment, as
        in the reference firmware. `on_text(text)` gets the reply so far.
        """
        await self._subscribe()
        turn = self._turn = _Turn()
        try:
            ack = await self.send_chat(message)
            if not ack["ok"]:
                raise OSError("the Muse did not take the message: HTTP %d" % ack["status"])
            response = ack["response"] if isinstance(ack["response"], dict) else {}
            result = response.get("result") if isinstance(response.get("result"), dict) else response
            turn.message_id = result.get("message_id")
            started = monotonic()
            shown = ""
            while True:
                await asyncio.sleep(TURN_POLL_S)
                text, done = turn.reply()
                if on_text and text != shown:
                    shown = text
                    on_text(text)
                now = monotonic()
                quiet = now - turn.last_event > REPLY_QUIET_S
                if text and done and quiet and not turn.busy:
                    return text
                if self._subscription.closed:
                    raise OSError("chat stream closed: " + self._subscription.error)
                if not text and now - started > first_reply_s:
                    raise OSError("the Muse did not reply")
                if now - started > TURN_CAP_S:
                    if text:
                        return text
                    raise OSError("the Muse did not finish replying")
        finally:
            self._turn = None

    async def send(self, message):
        async with self._send_lock:
            frames = self._transport.body_chunk(self._stream_id, encode_message(message))
            await self._send_frames(frames)

    async def _send_frames(self, frames):
        for frame in frames:
            await self._ws.send(frame)

    async def _read_loop(self):
        decoder = MessageDecoder()
        while True:
            try:
                raw = await self._ws.recv()
            except Exception as exc:
                self._log("control connection closed: %r" % (exc,))
                return CLOSED
            if isinstance(raw, str):
                continue
            frame = self._transport.decrypt_frame(raw)
            if frame is None:
                continue
            if frame.stream_id != self._stream_id:
                request = self._requests.get(frame.stream_id)
                if request is not None:
                    request.on_frame(frame)
                continue
            if frame.kind == "reset":
                self._log("control stream reset: " + frame.reason)
                return CLOSED
            if frame.kind == "response" and frame.status >= 400:
                self._log("/link-control refused: HTTP %d" % frame.status)
                return FORBIDDEN if frame.status == 403 else CLOSED
            for message in decoder.feed(frame.data):
                outcome = self._handle(message)
                if outcome is not None:
                    return outcome
            if frame.end_body:
                self._log("control stream ended by VM")
                return CLOSED

    def _handle(self, message):
        if message.get("id") == self._register_id and message.get("method") is None:
            if message.get("error"):
                self._log("link.register rejected: %s" % (message["error"],))
            else:
                self.registered_at = monotonic()
                self._log("registered with the Muse")
            return None
        if message.get("event") in ("link.unpaired", "node.unpaired"):
            self._log("the Muse removed this device")
            return UNPAIRED
        if message.get("method") == "link.invoke":
            self._tasks = [t for t in self._tasks if not t.done()]
            self._tasks.append(asyncio.create_task(self._invoke(message)))
        return None

    async def _invoke(self, message):
        invoke_id = message.get("id")
        if not invoke_id:
            return
        command = message.get("command") or ""
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        self._log("invoke " + command)
        try:
            result = await self._run_command(command, params, message.get("timeout_ms") or None)
        except Exception as exc:
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        reply = {"method": "link.result", "id": invoke_id}
        reply.update(result)
        await self.send(reply)
