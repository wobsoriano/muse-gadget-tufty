"""Async HTTP and WebSocket clients over asyncio streams.

MicroPython's bundled `requests` blocks the event loop and skips certificate
checks, and its WebSocket helper cannot send an Authorization header, so both
clients are written here against the stream API that MicroPython and CPython
share.
"""

import asyncio
import binascii
import hashlib
import os

from .compat import monotonic

MAX_WS_MESSAGE = 4 * 1024 * 1024
MAX_HTTP_BODY = 256 * 1024
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class UpgradeRejected(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def split_url(url):
    scheme, rest = url.split("://", 1)
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    secure = scheme in ("https", "wss")
    return secure, host, int(port) if port else (443 if secure else 80), "/" + path


async def _open(url, ssl):
    secure, host, port, path = split_url(url)
    if secure:
        reader, writer = await asyncio.open_connection(
            host, port, ssl=ssl or True, server_hostname=host)
    else:
        reader, writer = await asyncio.open_connection(host, port)
    return reader, writer, host, path


async def _close(writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def _read_head(reader):
    status_line = await reader.readline()
    parts = status_line.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise OSError("malformed HTTP response")
    status = int(parts[1])
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


def _head(method, path, host, headers):
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: " + host]
    for name, value in headers.items():
        lines.append("%s: %s" % (name, value))
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _read_body(reader, headers):
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = bytearray()
        while True:
            size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
            if size == 0:
                break
            if len(body) + size > MAX_HTTP_BODY:
                raise OSError("HTTP body too large")
            body += await reader.readexactly(size)
            await reader.readline()
        return bytes(body)
    length = headers.get("content-length")
    if length is not None:
        if int(length) > MAX_HTTP_BODY:
            raise OSError("HTTP body too large")
        return await reader.readexactly(int(length)) if int(length) else b""
    return await reader.read(MAX_HTTP_BODY)


async def http_request(method, url, headers=None, body=None, ssl=None, timeout=15):
    """One request on a fresh connection; returns (status, body bytes).

    Raises OSError or asyncio.TimeoutError when no HTTP response arrives.
    """
    async def exchange():
        reader, writer, host, path = await _open(url, ssl)
        try:
            request_headers = {"Connection": "close"}
            request_headers.update(headers or {})
            if body is not None:
                request_headers["Content-Length"] = str(len(body))
            writer.write(_head(method, path, host, request_headers))
            if body:
                writer.write(body)
            await writer.drain()
            status, response_headers = await _read_head(reader)
            return status, await _read_body(reader, response_headers)
        finally:
            await _close(writer)

    return await asyncio.wait_for(exchange(), timeout)


class WebSocket:
    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self.last_rx = monotonic()

    async def _write(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
        async with self._lock:
            # An all-zero mask leaves the payload unchanged, which saves a
            # byte-by-byte XOR in Python on every frame. Masking exists to
            # stop browsers poisoning plaintext proxies; under TLS it adds
            # nothing.
            self._writer.write(head + b"\x00\x00\x00\x00")
            if n:
                self._writer.write(payload)
            await self._writer.drain()

    async def send(self, data):
        if isinstance(data, str):
            await self._write(0x1, data.encode())
        else:
            await self._write(0x2, data)

    async def ping(self):
        await self._write(0x9, b"")

    async def recv(self):
        """Next complete message: bytes for binary, str for text."""
        reader = self._reader
        parts = None
        kind = 0
        total = 0
        while True:
            head = await reader.readexactly(2)
            fin = head[0] & 0x80
            opcode = head[0] & 0x0F
            n = head[1] & 0x7F
            if n == 126:
                n = int.from_bytes(await reader.readexactly(2), "big")
            elif n == 127:
                n = int.from_bytes(await reader.readexactly(8), "big")
            total += n
            if total > MAX_WS_MESSAGE:
                raise OSError("WebSocket message too large")
            mask = await reader.readexactly(4) if head[1] & 0x80 else None
            payload = await reader.readexactly(n) if n else b""
            if mask and mask != b"\x00\x00\x00\x00":
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            self.last_rx = monotonic()
            if opcode == 0x9:
                await self._write(0xA, payload)
            elif opcode == 0xA:
                pass
            elif opcode == 0x8:
                raise OSError("WebSocket closed by peer")
            elif opcode in (0x1, 0x2):
                kind = opcode
                parts = [payload]
            elif opcode == 0x0 and parts is not None:
                parts.append(payload)
            else:
                raise OSError("unexpected WebSocket frame")
            if fin and parts is not None and opcode < 0x8:
                data = parts[0] if len(parts) == 1 else b"".join(parts)
                return data.decode() if kind == 0x1 else data

    async def close(self):
        try:
            await asyncio.wait_for(self._write(0x8, b""), 2)
        except Exception:
            pass
        await _close(self._writer)


async def ws_connect(url, headers=None, ssl=None, timeout=20):
    """Open a WebSocket; raises UpgradeRejected if the server answers 401/403."""
    async def upgrade():
        reader, writer, host, path = await _open(url, ssl)
        try:
            key = binascii.b2a_base64(os.urandom(16)).strip()
            request_headers = {
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": key.decode(),
                "Sec-WebSocket-Version": "13",
            }
            request_headers.update(headers or {})
            writer.write(_head("GET", path, host, request_headers))
            await writer.drain()
            status, response_headers = await _read_head(reader)
            if status in (401, 403):
                raise UpgradeRejected(status)
            expected = binascii.b2a_base64(hashlib.sha1(key + _WS_GUID).digest()).strip()
            if status != 101 or response_headers.get("sec-websocket-accept", "").encode() != expected:
                raise OSError("WebSocket upgrade failed: HTTP %d" % status)
            return WebSocket(reader, writer)
        except Exception:
            await _close(writer)
            raise

    return await asyncio.wait_for(upgrade(), timeout)
