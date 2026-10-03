"""Small shims over the differences between MicroPython and CPython."""

import binascii
import os
import time

try:
    from time import ticks_diff, ticks_ms

    _last = ticks_ms()
    _elapsed_ms = 0

    def monotonic():
        # ticks_ms wraps, and time.time() jumps when NTP sets the clock.
        global _last, _elapsed_ms
        now = ticks_ms()
        _elapsed_ms += ticks_diff(now, _last)
        _last = now
        return _elapsed_ms / 1000

except ImportError:
    monotonic = time.monotonic


def uuid4():
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = binascii.hexlify(b).decode()
    return "-".join((h[:8], h[8:12], h[12:16], h[16:20], h[20:]))


def b64url_encode(data):
    text = binascii.b2a_base64(data).decode().strip().rstrip("=")
    return text.replace("+", "-").replace("/", "_")


_B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def b64url_decode(text, max_chars=4096):
    """Decode unpadded base64url, rejecting anything the firmware rejects."""
    if not isinstance(text, str) or not 0 < len(text) <= max_chars:
        raise ValueError("invalid base64url length")
    if len(text) % 4 == 1:
        raise ValueError("invalid base64url")
    for ch in text:
        if ch not in _B64URL:
            raise ValueError("invalid base64url")
    padded = text.replace("-", "+").replace("_", "/") + "=" * (-len(text) % 4)
    return binascii.a2b_base64(padded)


def compact_json(obj):
    import json
    return json.dumps(obj, separators=(",", ":"))
