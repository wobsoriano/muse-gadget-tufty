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
# Modified: ported from the Muse Gadget SDK's Linux client (linux/src/musegadget/noise/)
# to MicroPython for the Pimoroni Tufty 2350.

"""Client side of the Muse Noise session.

Noise_XX_25519_AESGCM_SHA256 handshake, chunked transport frames and the
service envelopes, ported from the Linux Device SDK's `noise` package. Only
the initiator and the client's half of the envelopes are here.
"""

import os
import time

from . import mcrypto
from .proto import (
    ProtoError,
    WIRE_DELIMITED,
    WIRE_VARINT,
    bool_field,
    bytes_field,
    decode_int32,
    decode_int64,
    decode_uint32,
    delimited_field,
    int32_field,
    int64_field,
    read_delimited,
    read_key,
    read_varint,
    skip_field,
    string_field,
    uint32_field,
)

PROTOCOL_NAME = b"Noise_XX_25519_AESGCM_SHA256"
DH_KEY_LEN = 32
AEAD_TAG_LEN = 16
MIN_MSG2_LEN = DH_KEY_LEN + (DH_KEY_LEN + AEAD_TAG_LEN) + AEAD_TAG_LEN
MAX_SAFE_NONCE = (1 << 53) - 1

MAX_CHUNK_PAYLOAD = 65489
MAX_PENDING_ASSEMBLIES = 16
MAX_TOTAL_CHUNKS = 256
MAX_ASSEMBLY_BYTES = 16 * 1024 * 1024
ASSEMBLY_TTL_SECONDS = 60

RESET_CANCELLED = 1

_LOW_ORDER_POINTS = tuple(bytes.fromhex(h) for h in (
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
    "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
))


class NoiseError(Exception):
    pass


def _hkdf2(chaining_key, ikm):
    temp_key = mcrypto.hmac_sha256(chaining_key, ikm)
    out1 = mcrypto.hmac_sha256(temp_key, b"\x01")
    out2 = mcrypto.hmac_sha256(temp_key, out1 + b"\x02")
    return out1, out2


class CipherState:
    def __init__(self, key=None):
        self._gcm = mcrypto.AESGCM(key) if key is not None else None
        self._nonce = 0
        self._poisoned = False

    def _next_iv(self):
        if self._poisoned:
            raise NoiseError("cipher poisoned after prior failure")
        if self._nonce >= MAX_SAFE_NONCE:
            self._poisoned = True
            raise NoiseError("nonce exhausted")
        iv = b"\x00\x00\x00\x00" + self._nonce.to_bytes(8, "big")
        self._nonce += 1
        return iv

    def encrypt(self, ad, plaintext):
        if self._gcm is None:
            return plaintext
        return self._gcm.encrypt(self._next_iv(), plaintext, ad)

    def decrypt(self, ad, ciphertext):
        if self._gcm is None:
            return ciphertext
        iv = self._next_iv()
        try:
            return self._gcm.decrypt(iv, ciphertext, ad)
        except mcrypto.InvalidTag:
            self._poisoned = True
            raise NoiseError("decrypt failed")


class _SymmetricState:
    def __init__(self):
        self.h = PROTOCOL_NAME + bytes(32 - len(PROTOCOL_NAME))
        self.ck = self.h
        self.cipher = CipherState()
        self.mix_hash(b"")

    def mix_hash(self, data):
        self.h = mcrypto.sha256(self.h + data)

    def mix_key(self, ikm):
        self.ck, temp_k = _hkdf2(self.ck, ikm)
        self.cipher = CipherState(temp_k)

    def encrypt_and_hash(self, plaintext):
        ciphertext = self.cipher.encrypt(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext):
        plaintext = self.cipher.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self):
        k1, k2 = _hkdf2(self.ck, b"")
        return CipherState(k1), CipherState(k2)


def _dh(private, public):
    if len(public) != DH_KEY_LEN:
        raise NoiseError("x25519: invalid public key length")
    if public in _LOW_ORDER_POINTS:
        raise NoiseError("x25519: rejected low-order public key")
    shared = mcrypto.x25519(private, public)
    if shared == bytes(32):
        raise NoiseError("x25519: DH produced all-zeros output")
    return shared


def generate_keypairs():
    """The initiator's ephemeral and static pairs.

    Generating them costs two scalar multiplications, which are slow on a
    microcontroller, so callers make them before the socket is open and the
    server's handshake clock is running.
    """
    return mcrypto.x25519_generate(), mcrypto.x25519_generate()


class NoiseXXInitiator:
    def __init__(self, keypairs=None):
        self._e, self._s = keypairs or generate_keypairs()
        self._ss = _SymmetricState()
        self._re = None
        self._step = 0

    def _expect(self, step):
        if self._step != step:
            raise NoiseError("handshake step out of order")
        self._step = -1

    def write_message1(self):
        self._expect(0)
        self._ss.mix_hash(self._e[1])
        self._ss.encrypt_and_hash(b"")
        self._step = 1
        return self._e[1]

    def read_message2(self, msg):
        self._expect(1)
        if len(msg) < MIN_MSG2_LEN:
            raise NoiseError("message 2 too short")
        self._re = bytes(msg[:DH_KEY_LEN])
        self._ss.mix_hash(self._re)
        self._ss.mix_key(_dh(self._e[0], self._re))
        end = DH_KEY_LEN + DH_KEY_LEN + AEAD_TAG_LEN
        rs = self._ss.decrypt_and_hash(bytes(msg[DH_KEY_LEN:end]))
        self._ss.mix_key(_dh(self._e[0], rs))
        payload = self._ss.decrypt_and_hash(bytes(msg[end:]))
        self._step = 2
        return payload

    def write_message3(self):
        self._expect(2)
        enc_s = self._ss.encrypt_and_hash(self._s[1])
        self._ss.mix_key(_dh(self._s[0], self._re))
        enc_payload = self._ss.encrypt_and_hash(b"")
        self._step = 3
        return enc_s + enc_payload

    def split(self):
        self._expect(3)
        self._e = self._s = self._re = None
        return self._ss.split()


def _random_int64():
    value = int.from_bytes(os.urandom(8), "big")
    return value - (1 << 64) if value >= 1 << 63 else value


def encode_noise_frames(payload):
    chunk_id = _random_int64()
    total = max(1, (len(payload) + MAX_CHUNK_PAYLOAD - 1) // MAX_CHUNK_PAYLOAD)
    if total > MAX_TOTAL_CHUNKS:
        raise ValueError("payload too large for noise framing")
    frames = []
    for index in range(total):
        chunk = payload[index * MAX_CHUNK_PAYLOAD:(index + 1) * MAX_CHUNK_PAYLOAD]
        out = bytearray()
        if chunk_id:
            out += int64_field(1, chunk_id)
        if index:
            out += uint32_field(2, index)
        out += uint32_field(3, total)
        if chunk:
            out += bytes_field(4, chunk)
        frames.append(bytes(out))
    return frames


class NoiseFrameDecoder:
    def __init__(self):
        self._pending = {}

    def decode(self, data):
        chunk_id, index, total, payload = 0, 0, 1, b""
        offset = 0
        while offset < len(data):
            field, wire, offset = read_key(data, offset)
            if field in (1, 2, 3):
                if wire != WIRE_VARINT:
                    raise ProtoError("NoiseTransportFrame wrong wire type")
                raw, offset = read_varint(data, offset)
                if field == 1:
                    chunk_id = decode_int64(raw)
                elif field == 2:
                    index = decode_uint32(raw)
                else:
                    total = decode_uint32(raw)
            elif field == 4:
                if wire != WIRE_DELIMITED:
                    raise ProtoError("NoiseTransportFrame wrong wire type")
                payload, offset = read_delimited(data, offset)
            else:
                offset = skip_field(data, offset, wire)

        if total < 1 or total > MAX_TOTAL_CHUNKS:
            raise ValueError("invalid totalChunks")
        if index >= total:
            raise ValueError("chunkIndex out of range")
        if len(payload) > MAX_CHUNK_PAYLOAD:
            raise ValueError("payload too large for noise frame")
        if total == 1:
            return bytes(payload)

        now = time.time()
        for stale in [k for k, a in self._pending.items() if now - a[3] > ASSEMBLY_TTL_SECONDS]:
            del self._pending[stale]
        assembly = self._pending.get(chunk_id)
        if assembly is None:
            if len(self._pending) >= MAX_PENDING_ASSEMBLIES:
                raise ValueError("too many pending noise frame assemblies")
            assembly = self._pending[chunk_id] = [{}, total, 0, now]
        chunks = assembly[0]
        if assembly[1] != total or index in chunks:
            del self._pending[chunk_id]
            raise ValueError("inconsistent noise frame assembly")
        assembly[2] += len(payload)
        if assembly[2] > MAX_ASSEMBLY_BYTES:
            del self._pending[chunk_id]
            raise ValueError("assembly exceeded byte budget")
        chunks[index] = bytes(payload)
        if len(chunks) < total:
            return None
        del self._pending[chunk_id]
        return b"".join(chunks[i] for i in range(total))


class Frame:
    """One decoded service frame from the VM.

    kind is "response", "body_chunk" or "reset". A response carries status
    and its first body bytes in data; a reset carries reason.
    """

    def __init__(self, kind, stream_id, status=0, data=b"", end_body=False, reason=""):
        self.kind = kind
        self.stream_id = stream_id
        self.status = status
        self.data = data
        self.end_body = end_body
        self.reason = reason


def _encode_request(verb, path, headers, body, end_body):
    out = bytearray()
    out += string_field(1, verb)
    out += string_field(2, path)
    for key, value in headers:
        out += delimited_field(3, string_field(1, key) + string_field(2, value))
    if body:
        out += bytes_field(4, body)
    if end_body:
        out += bool_field(5, True)
    return bytes(out)


def _encode_body_chunk(data, end_body):
    out = bytearray()
    if data:
        out += bytes_field(1, data)
    if end_body:
        out += bool_field(2, True)
    return bytes(out)


def _encode_reset(code, reason):
    out = bytearray()
    if code:
        out += int32_field(1, code)
    if reason:
        out += string_field(2, reason)
    return bytes(out)


def _encode_service_request(stream_id, field, message):
    frame = bytearray()
    if stream_id:
        frame += int64_field(1, stream_id)
    frame += delimited_field(field, message)
    # ServiceRequest.service is SERVICE_DAEMON (0), which proto3 omits.
    return bytes_field(2, bytes(frame))


def _decode_fields(data, varints, delimited):
    """Decode one message into {field: value}, by declared wire type."""
    values = {}
    offset = 0
    while offset < len(data):
        field, wire, offset = read_key(data, offset)
        if field in varints:
            if wire != WIRE_VARINT:
                raise ProtoError("wrong wire type")
            values[field], offset = read_varint(data, offset)
        elif field in delimited:
            if wire != WIRE_DELIMITED:
                raise ProtoError("wrong wire type")
            values[field], offset = read_delimited(data, offset)
        else:
            offset = skip_field(data, offset, wire)
    return values


def decode_service_response(data):
    payload = _decode_fields(data, (), (1,)).get(1, b"")
    if not payload:
        raise NoiseError("empty ServiceResponse payload")
    frame = _decode_fields(payload, (1,), (2, 3, 4, 5))
    stream_id = decode_int64(frame.get(1, 0))
    if 2 in frame:
        raise NoiseError("unexpected request frame from server")
    if 3 in frame:
        response = _decode_fields(frame[3], (1, 4), (3,))
        return Frame(
            "response", stream_id,
            status=decode_int32(response.get(1, 0)),
            data=bytes(response.get(3, b"")),
            end_body=bool(response.get(4, 0)),
        )
    if 4 in frame:
        chunk = _decode_fields(frame[4], (2,), (1,))
        return Frame("body_chunk", stream_id, data=bytes(chunk.get(1, b"")),
                     end_body=bool(chunk.get(2, 0)))
    if 5 in frame:
        reset = _decode_fields(frame[5], (1,), (2,))
        return Frame("reset", stream_id, reason=bytes(reset.get(2, b"")).decode())
    return None


class NoiseTransport:
    def __init__(self, send, recv):
        self._send = send
        self._recv = recv
        self._decoder = NoiseFrameDecoder()
        self._next_stream_id = 1

    def _seal(self, stream_id, field, message):
        request = _encode_service_request(stream_id, field, message)
        return [self._send.encrypt(b"", f) for f in encode_noise_frames(request)]

    def request(self, verb, path, body=b"", headers=(), end_body=True):
        """Open a stream; returns (stream_id, encrypted frames)."""
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        message = _encode_request(verb, path, headers, body, end_body)
        return stream_id, self._seal(stream_id, 2, message)

    def body_chunk(self, stream_id, data, end_body=False):
        return self._seal(stream_id, 4, _encode_body_chunk(data, end_body))

    def reset(self, stream_id, reason="", code=RESET_CANCELLED):
        return self._seal(stream_id, 5, _encode_reset(code, reason))

    def decrypt_frame(self, ciphertext):
        assembled = self._decoder.decode(self._recv.decrypt(b"", ciphertext))
        if assembled is None:
            return None
        return decode_service_response(assembled)
