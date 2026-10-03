import hashlib
import os
import struct

try:
    from cryptolib import aes as _aes

    def _ecb_encryptor(key):
        return _aes(key, 1).encrypt

except ImportError:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def _ecb_encryptor(key):
        return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update


class InvalidTag(Exception):
    pass


_yield_hook = None


def set_yield_hook(fn):
    global _yield_hook
    _yield_hook = fn


def random_bytes(n):
    return os.urandom(n)


def sha256(data):
    return hashlib.sha256(data).digest()


def hmac_sha256(key, msg):
    if len(key) > 64:
        key = sha256(key)
    key = bytes(key) + bytes(64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key))
    inner.update(msg)
    outer = hashlib.sha256(bytes(b ^ 0x5C for b in key))
    outer.update(inner.digest())
    return outer.digest()


def hkdf_extract(salt, ikm):
    return hmac_sha256(salt if salt else bytes(32), ikm)


def hkdf_expand(prk, info, length):
    if length > 255 * 32:
        raise ValueError("hkdf length too large")
    info = bytes(info) if info else b""
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac_sha256(prk, block + info + bytes((counter,)))
        okm += block
        counter += 1
    return okm[:length]


def hkdf(ikm, salt, info, length):
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)


_P25519 = (1 << 255) - 19
_A24 = 121665


def x25519(private32, peer_public32):
    if len(private32) != 32 or len(peer_public32) != 32:
        raise ValueError("x25519 needs 32-byte inputs")
    p = _P25519
    k = int.from_bytes(private32, "little")
    k = (k & ((1 << 254) - 8)) | (1 << 254)
    x1 = (int.from_bytes(peer_public32, "little") & ((1 << 255) - 1)) % p
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    hook = _yield_hook
    t = 254
    while t >= 0:
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = x2 + z2
        b = x2 - z2
        aa = a * a % p
        bb = b * b % p
        e = aa - bb
        da = (x3 - z3) * a % p
        cb = (x3 + z3) * b % p
        x3 = da + cb
        x3 = x3 * x3 % p
        z3 = da - cb
        z3 = x1 * (z3 * z3 % p) % p
        x2 = aa * bb % p
        z2 = e * (aa + _A24 * e) % p
        if hook and not t & 15:
            hook()
        t -= 1
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, "little")


_X25519_BASE = b"\x09" + bytes(31)


def x25519_public(private32):
    return x25519(private32, _X25519_BASE)


def x25519_generate():
    private = random_bytes(32)
    return private, x25519_public(private)


_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


def _p256_mul(k, px, py):
    if not 0 < k < _P256_N:
        raise ValueError("p256 scalar out of range")
    p = _P256_P
    hook = _yield_hook
    i = 255
    while not (k >> i) & 1:
        i -= 1
    x, y, z = px, py, 1
    i -= 1
    while i >= 0:
        delta = z * z % p
        gamma = y * y % p
        beta = x * gamma % p
        alpha = 3 * (x - delta) * (x + delta) % p
        z = ((y + z) * (y + z) - gamma - delta) % p
        x = (alpha * alpha - 8 * beta) % p
        y = (alpha * (4 * beta - x) - 8 * gamma * gamma) % p
        if (k >> i) & 1:
            zz = z * z % p
            h = (px * zz - x) % p
            r = (py * z % p * zz - y) % p
            # h == 0 needs the running point to be +-P, which a scalar in
            # [1, n-1] on a prime-order curve never reaches after a doubling.
            hh = h * h % p
            hhh = h * hh % p
            v = x * hh % p
            x = (r * r - hhh - 2 * v) % p
            y = (r * (v - x) - y * hhh) % p
            z = z * h % p
        if hook and not i & 15:
            hook()
        i -= 1
    if z == 0:
        raise ValueError("p256 point at infinity")
    zinv = pow(z, p - 2, p)
    zinv2 = zinv * zinv % p
    return x * zinv2 % p, y * zinv2 % p * zinv % p


def p256_public(private):
    x, y = _p256_mul(private, _P256_GX, _P256_GY)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def p256_generate():
    while True:
        private = int.from_bytes(random_bytes(32), "big")
        if 0 < private < _P256_N:
            return private, p256_public(private)


def p256_ecdh(private, peer_public65):
    if len(peer_public65) != 65 or peer_public65[0] != 4:
        raise ValueError("p256 public key must be a 65-byte uncompressed point")
    p = _P256_P
    x = int.from_bytes(peer_public65[1:33], "big")
    y = int.from_bytes(peer_public65[33:65], "big")
    if x >= p or y >= p or (y * y - (x * x - 3) * x - _P256_B) % p:
        raise ValueError("p256 point is not on the curve")
    return _p256_mul(private, x, y)[0].to_bytes(32, "big")


_GCM_CHUNK = 1024
_GCM_POLY = 0xE1 << 120


def _gf_mulx(v):
    return (v >> 1) ^ _GCM_POLY if v & 1 else v >> 1


def _gf_span(values):
    # Extends 8 single-bit entries (indices 1, 2, 4, ...) to all 256 by linearity.
    table = [0] * 256
    for bit in range(8):
        base = 1 << bit
        table[base] = values[bit]
        for i in range(1, base):
            table[base + i] = values[bit] ^ table[i]
    return table


def _gcm_reduce_table():
    values = []
    for bit in range(8):
        v = 1 << bit
        for _ in range(8):
            v = _gf_mulx(v)
        values.append(v)
    return _gf_span(values)


_GCM_REDUCE = _gcm_reduce_table()


class AESGCM:
    def __init__(self, key):
        if len(key) not in (16, 32):
            raise ValueError("AESGCM key must be 16 or 32 bytes")
        self._ecb = _ecb_encryptor(bytes(key))
        h = int.from_bytes(self._ecb(bytes(16)), "big")
        values = [0] * 8
        for bit in range(7, -1, -1):
            values[bit] = h
            h = _gf_mulx(h)
        self._table = _gf_span(values)

    def _ghash(self, y, data):
        table = self._table
        reduce = _GCM_REDUCE
        hook = _yield_hook
        mv = memoryview(data)
        n = len(data)
        full = n - n % 16
        o = 0
        while o < n:
            if o < full:
                block = mv[o:o + 16]
            else:
                block = bytes(mv[o:]) + bytes(16 - n % 16)
            x = y ^ int.from_bytes(block, "big")
            y = 0
            # Little-endian yields the highest-degree byte first, which is Horner order.
            for b in x.to_bytes(16, "little"):
                y = (y >> 8) ^ reduce[y & 255] ^ table[b]
            o += 16
            if hook and not o & 1023:
                hook()
        return y

    def _ctr(self, nonce, data, out):
        hook = _yield_hook
        ecb = self._ecb
        mv = memoryview(data)
        n = len(data)
        blocks = bytearray((bytes(nonce) + bytes(4)) * min(_GCM_CHUNK // 16, (n + 15) // 16))
        counter = 2
        o = 0
        while o < n:
            size = min(_GCM_CHUNK, n - o)
            used = (size + 15) // 16 * 16
            for off in range(12, used, 16):
                struct.pack_into(">I", blocks, off, counter)
                counter += 1
            stream = ecb(bytes(memoryview(blocks)[:used]))
            if size != used:
                stream = stream[:size]
            mixed = int.from_bytes(stream, "big") ^ int.from_bytes(mv[o:o + size], "big")
            out[o:o + size] = mixed.to_bytes(size, "big")
            o += size
            if hook:
                hook()

    def _tag(self, nonce, aad, ciphertext):
        y = self._ghash(0, aad)
        y = self._ghash(y, ciphertext)
        y = self._ghash(y, struct.pack(">QQ", len(aad) * 8, len(ciphertext) * 8))
        mask = self._ecb(bytes(nonce) + b"\x00\x00\x00\x01")
        return (y ^ int.from_bytes(mask, "big")).to_bytes(16, "big")

    def encrypt(self, nonce12, plaintext, aad):
        if len(nonce12) != 12:
            raise ValueError("AESGCM nonce must be 12 bytes")
        n = len(plaintext)
        out = bytearray(n + 16)
        self._ctr(nonce12, plaintext, out)
        out[n:] = self._tag(nonce12, aad or b"", memoryview(out)[:n])
        return bytes(out)

    def decrypt(self, nonce12, data, aad):
        if len(nonce12) != 12:
            raise ValueError("AESGCM nonce must be 12 bytes")
        n = len(data) - 16
        if n < 0:
            raise InvalidTag()
        mv = memoryview(data)
        expected = self._tag(nonce12, aad or b"", mv[:n])
        diff = 0
        for i in range(16):
            diff |= expected[i] ^ mv[n + i]
        if diff:
            raise InvalidTag()
        out = bytearray(n)
        self._ctr(nonce12, mv[:n], out)
        return bytes(out)
