import sys
import time

# On the badge the package is importable as-is; from a checkout it lives in src.
try:
    from musebadge import mcrypto
except ImportError:
    _here = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
    sys.path.insert(0, _here + "/../app/muse")
    from musebadge import mcrypto

if hasattr(time, "ticks_ms"):
    _now = time.ticks_ms

    def _elapsed(start):
        return time.ticks_diff(time.ticks_ms(), start)

else:
    _now = time.time

    def _elapsed(start):
        return (time.time() - start) * 1000


def bench(label, fn):
    runs = 1
    while True:
        start = _now()
        for _ in range(runs):
            fn()
        elapsed = _elapsed(start)
        if elapsed >= 250 or runs >= 4096:
            break
        runs *= 4
    print("%-24s %10.3f ms  (%d runs)" % (label, elapsed / runs, runs))


def main():
    x_private, x_public = mcrypto.x25519_generate()
    p_private, p_public = mcrypto.p256_generate()
    key = bytes(range(32))
    nonce = bytes(12)
    aad = b"hatch-link ble setup v1|session|m2d|0"
    cipher = mcrypto.AESGCM(key)

    bench("x25519 scalar mult", lambda: mcrypto.x25519(x_private, x_public))
    bench("p256_generate", mcrypto.p256_generate)
    bench("p256_ecdh", lambda: mcrypto.p256_ecdh(p_private, p_public))
    bench("AESGCM key setup (256)", lambda: mcrypto.AESGCM(key))
    for label, size in (("64 B", 64), ("1 KB", 1024), ("16 KB", 16384)):
        data = bytes(size)
        bench("GCM encrypt " + label, lambda: cipher.encrypt(nonce, data, aad))


main()
