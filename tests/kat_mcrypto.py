import binascii
import json
import sys

_HERE = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
sys.path.insert(0, _HERE + "/../app/muse")

from musebadge import mcrypto

VECTOR_PATH = _HERE + "/../vendor/muse-gadget-sdk/linux/tests/vectors/link_pairing_v5.json"


def unhex(text):
    return binascii.unhexlify(text)


def unb64url(text):
    text = text.replace("-", "+").replace("_", "/")
    return binascii.a2b_base64(text + "=" * (-len(text) % 4))


def eq(got, want, what):
    if got != want:
        raise AssertionError("%s: got %r want %r" % (what, got, want))


def raises(exc, fn, what):
    try:
        fn()
    except exc:
        return
    raise AssertionError("%s: expected %s" % (what, exc.__name__))


def kat_sha256():
    eq(
        mcrypto.sha256(b"abc"),
        unhex("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
        "sha256 abc",
    )


# RFC 4231 test cases 1, 2, 3, 6: (key, data, mac)
HMAC_VECTORS = (
    (b"\x0b" * 20, b"Hi There",
     "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"),
    (b"Jefe", b"what do ya want for nothing?",
     "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
    (b"\xaa" * 20, b"\xdd" * 50,
     "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe"),
    (b"\xaa" * 131, b"Test Using Larger Than Block-Size Key - Hash Key First",
     "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"),
)


def kat_hmac():
    for key, data, mac in HMAC_VECTORS:
        eq(mcrypto.hmac_sha256(key, data), unhex(mac), "hmac rfc4231")


# RFC 5869 test cases 1, 2, 3: (ikm, salt, info, length, prk, okm)
HKDF_VECTORS = (
    (b"\x0b" * 22, bytes(range(13)), bytes(range(0xF0, 0xFA)), 42,
     "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5",
     "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
     "34007208d5b887185865"),
    (bytes(range(0x50)), bytes(range(0x60, 0xB0)), bytes(range(0xB0, 0x100)), 82,
     "06a6b88c5853361a06104c9ceb35b45cef760014904671014a193f40c15fc244",
     "b11e398dc80327a1c8e7f78c596a49344f012eda2d4efad8a050cc4c19afa97c"
     "59045a99cac7827271cb41c65e590e09da3275600c2f09b8367793a9aca3db71"
     "cc30c58179ec3e87c14c01d5c1f3434f1d87"),
    (b"\x0b" * 22, b"", b"", 42,
     "19ef24a32c717b167f33a91d6f648bdf96596776afdb6377ac434c1c293ccb04",
     "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
     "9d201395faa4b61a96c8"),
)


def kat_hkdf():
    for ikm, salt, info, length, prk, okm in HKDF_VECTORS:
        eq(mcrypto.hkdf_extract(salt, ikm), unhex(prk), "hkdf extract")
        eq(mcrypto.hkdf_expand(unhex(prk), info, length), unhex(okm), "hkdf expand")
        eq(mcrypto.hkdf(ikm, salt, info, length), unhex(okm), "hkdf")
    eq(mcrypto.hkdf_extract(None, b"\x0b" * 22), unhex(HKDF_VECTORS[2][4]), "hkdf salt None")
    raises(ValueError, lambda: mcrypto.hkdf_expand(bytes(32), b"", 255 * 32 + 1), "hkdf too long")


# RFC 7748 section 5.2: (scalar, u, output)
X25519_VECTORS = (
    ("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4",
     "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c",
     "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"),
    ("4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d",
     "e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493",
     "95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957"),
)


def kat_x25519_scalarmult():
    for scalar, u, out in X25519_VECTORS:
        eq(mcrypto.x25519(unhex(scalar), unhex(u)), unhex(out), "x25519 rfc7748 5.2")


def kat_x25519_iterated():
    k = u = b"\x09" + bytes(31)
    for i in range(1000):
        k, u = mcrypto.x25519(k, u), k
        if i == 0:
            eq(k, unhex("422c8e7a6227d7bca1350b3e2bb7279f7897b87bb6854b783c60e80311ae3079"),
               "x25519 1 iteration")
    eq(k, unhex("684cf59ba83309552800ef566f2f4d3c1c3887c49360e3875f2eb94d99532c51"),
       "x25519 1000 iterations")


def kat_x25519_dh():
    alice = unhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    alice_pub = unhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    bob = unhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    bob_pub = unhex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
    shared = unhex("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")
    eq(mcrypto.x25519_public(alice), alice_pub, "x25519 alice public")
    eq(mcrypto.x25519_public(bob), bob_pub, "x25519 bob public")
    eq(mcrypto.x25519(alice, bob_pub), shared, "x25519 alice shared")
    eq(mcrypto.x25519(bob, alice_pub), shared, "x25519 bob shared")


def kat_x25519_generate():
    private_a, public_a = mcrypto.x25519_generate()
    private_b, public_b = mcrypto.x25519_generate()
    eq(len(private_a), 32, "x25519 private length")
    eq(mcrypto.x25519(private_a, public_b), mcrypto.x25519(private_b, public_a), "x25519 agreement")


def kat_x25519_negative():
    raises(ValueError, lambda: mcrypto.x25519(bytes(31), bytes(32)), "x25519 short private")
    raises(ValueError, lambda: mcrypto.x25519(bytes(32), bytes(33)), "x25519 long public")
    eq(mcrypto.x25519(b"\x01" * 32, bytes(32)), bytes(32), "x25519 low-order point gives zeros")


# RFC 5903 section 8.1
P256_I = 0xC88F01F510D9AC3F70A292DAA2316DE544E9AAB8AFE84049C62A9C57862D1433
P256_GI = (
    "04"
    "dad0b65394221cf9b051e1feca5787d098dfe637fc90b9ef945d0c3772581180"
    "5271a0461cdb8252d61f1c456fa3e59ab1f45b33accf5f58389e0577b8990bb3"
)
P256_R = 0xC6EF9C5D78AE012A011164ACB397CE2088685D8F06BF9BE0B283AB46476BEE53
P256_GR = (
    "04"
    "d12dfb5289c8d4f81208b70270398c342296970a0bccb74c736fc7554494bf63"
    "56fbf3ca366cc23e8157854c13c58d6aac23f046ada30f8353e74f33039872ab"
)
P256_SHARED = "d6840f6b42f6edafd13116e0e12565202fef8e9ece7dce03812464d04b9442de"
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def kat_p256_ecdh():
    eq(mcrypto.p256_public(P256_I), unhex(P256_GI), "p256 initiator public")
    eq(mcrypto.p256_public(P256_R), unhex(P256_GR), "p256 responder public")
    eq(mcrypto.p256_ecdh(P256_I, unhex(P256_GR)), unhex(P256_SHARED), "p256 initiator shared")
    eq(mcrypto.p256_ecdh(P256_R, unhex(P256_GI)), unhex(P256_SHARED), "p256 responder shared")


def kat_p256_generate():
    private_a, public_a = mcrypto.p256_generate()
    private_b, public_b = mcrypto.p256_generate()
    eq(len(public_a), 65, "p256 public length")
    eq(public_a[0], 4, "p256 public prefix")
    eq(mcrypto.p256_ecdh(private_a, public_b), mcrypto.p256_ecdh(private_b, public_a),
       "p256 agreement")


def kat_p256_negative():
    good = unhex(P256_GR)
    off_curve = good[:64] + bytes((good[64] ^ 1,))
    cases = (
        ("off curve", off_curve),
        ("zero point", b"\x04" + bytes(64)),
        ("short", good[:64]),
        ("long", good + b"\x00"),
        ("empty", b""),
        ("compressed prefix", b"\x02" + good[1:]),
        ("infinity encoding", b"\x00"),
        ("coordinate not below p", b"\x04" + b"\xff" * 32 + good[33:]),
    )
    for name, point in cases:
        raises(ValueError, lambda: mcrypto.p256_ecdh(P256_I, point), "p256 " + name)
    for scalar in (0, P256_N, P256_N + 1, -1):
        raises(ValueError, lambda: mcrypto.p256_ecdh(scalar, good), "p256 scalar %d" % scalar)
        raises(ValueError, lambda: mcrypto.p256_public(scalar), "p256 public scalar %d" % scalar)
    eq(
        mcrypto.p256_ecdh(P256_N - 1, good),
        good[1:33],
        "p256 scalar n-1 keeps x",
    )


_GCM_K128 = "feffe9928665731c6d6a8f9467308308"
_GCM_IV = "cafebabefacedbaddecaf888"
_GCM_AAD = "feedfacedeadbeeffeedfacedeadbeefabaddad2"
_GCM_P64 = (
    "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
    "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255"
)
_GCM_C128 = (
    "42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
    "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091473f5985"
)
_GCM_C256 = (
    "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
    "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662898015ad"
)

# McGrew-Viega GCM spec test cases 1-4 and 13-16: (key, iv, plaintext, aad, ciphertext, tag)
GCM_VECTORS = (
    ("00" * 16, "00" * 12, "", "", "", "58e2fccefa7e3061367f1d57a4e7455a"),
    ("00" * 16, "00" * 12, "00" * 16, "",
     "0388dace60b6a392f328c2b971b2fe78", "ab6e47d42cec13bdf53a67b21257bddf"),
    (_GCM_K128, _GCM_IV, _GCM_P64, "", _GCM_C128, "4d5c2af327cd64a62cf35abd2ba6fab4"),
    (_GCM_K128, _GCM_IV, _GCM_P64[:120], _GCM_AAD, _GCM_C128[:120],
     "5bc94fbc3221a5db94fae95ae7121a47"),
    ("00" * 32, "00" * 12, "", "", "", "530f8afbc74536b9a963b4f1c4cb738b"),
    ("00" * 32, "00" * 12, "00" * 16, "",
     "cea7403d4d606b6e074ec5d3baf39d18", "d0d1c8a799996bf0265b98b5d48ab919"),
    (_GCM_K128 * 2, _GCM_IV, _GCM_P64, "", _GCM_C256, "b094dac5d93471bdec1a502270e3cc6c"),
    (_GCM_K128 * 2, _GCM_IV, _GCM_P64[:120], _GCM_AAD, _GCM_C256[:120],
     "76fc6ece0f4e1768cddf8853bb2d551b"),
)


def kat_gcm():
    for key, iv, plaintext, aad, ciphertext, tag in GCM_VECTORS:
        cipher = mcrypto.AESGCM(unhex(key))
        sealed = unhex(ciphertext) + unhex(tag)
        eq(cipher.encrypt(unhex(iv), unhex(plaintext), unhex(aad)), sealed, "gcm encrypt")
        eq(cipher.decrypt(unhex(iv), sealed, unhex(aad)), unhex(plaintext), "gcm decrypt")
    cipher = mcrypto.AESGCM(bytes(16))
    eq(cipher.encrypt(bytes(12), b"", None), unhex(GCM_VECTORS[0][5]), "gcm aad None")


def kat_gcm_multi_chunk():
    cipher = mcrypto.AESGCM(bytes(range(32)))
    nonce = bytes(range(12))
    for size in (1023, 1024, 1025, 2048, 2500):
        plaintext = bytes(i * 7 & 255 for i in range(size))
        sealed = cipher.encrypt(nonce, plaintext, b"header")
        eq(len(sealed), size + 16, "gcm sealed length")
        eq(cipher.decrypt(nonce, sealed, b"header"), plaintext, "gcm round trip %d" % size)
        eq(sealed[:64], cipher.encrypt(nonce, plaintext[:64], b"header")[:64], "gcm ctr prefix")


def kat_gcm_negative():
    cipher = mcrypto.AESGCM(bytes(range(32)))
    nonce = bytes(range(12))
    sealed = cipher.encrypt(nonce, b"attack at dawn, bring snacks", b"aad")
    for i in (0, 5, len(sealed) - 17, len(sealed) - 16, len(sealed) - 1):
        tampered = bytearray(sealed)
        tampered[i] ^= 1
        raises(mcrypto.InvalidTag, lambda: cipher.decrypt(nonce, bytes(tampered), b"aad"),
               "gcm tamper byte %d" % i)
    raises(mcrypto.InvalidTag, lambda: cipher.decrypt(nonce, sealed, b"aaD"), "gcm tamper aad")
    raises(mcrypto.InvalidTag, lambda: cipher.decrypt(nonce, sealed, None), "gcm dropped aad")
    raises(mcrypto.InvalidTag, lambda: cipher.decrypt(bytes(12), sealed, b"aad"), "gcm wrong nonce")
    raises(mcrypto.InvalidTag, lambda: cipher.decrypt(nonce, sealed[:-1], b"aad"), "gcm truncated")
    raises(mcrypto.InvalidTag, lambda: cipher.decrypt(nonce, sealed[:15], b"aad"), "gcm short")
    raises(mcrypto.InvalidTag,
           lambda: mcrypto.AESGCM(bytes(32)).decrypt(nonce, sealed, b"aad"), "gcm wrong key")
    raises(ValueError, lambda: cipher.encrypt(bytes(11), b"", b""), "gcm nonce length")
    raises(ValueError, lambda: cipher.decrypt(bytes(16), sealed, b"aad"), "gcm nonce length")
    raises(ValueError, lambda: mcrypto.AESGCM(bytes(24)), "gcm key length")
    if not issubclass(mcrypto.InvalidTag, Exception):
        raise AssertionError("InvalidTag must subclass Exception")


def kat_yield_hook():
    calls = [0]

    def hook():
        calls[0] += 1

    mcrypto.set_yield_hook(hook)
    try:
        mcrypto.x25519_public(b"\x01" * 32)
        ladder = calls[0]
        mcrypto.p256_public(P256_I)
        p256 = calls[0] - ladder
        mcrypto.AESGCM(bytes(16)).encrypt(bytes(12), bytes(4096), bytes(2048))
        gcm = calls[0] - ladder - p256
    finally:
        mcrypto.set_yield_hook(None)
    eq(ladder, 16, "x25519 hook calls")
    eq(p256, 16, "p256 hook calls")
    eq(gcm, 4 + 4 + 2, "gcm hook calls")
    mcrypto.x25519_public(b"\x01" * 32)
    eq(calls[0], ladder + p256 + gcm, "hook cleared")


RECORD_LABEL = b"hatch-link ble setup v1"
SESSION_ID_LABEL = b"hatch-link session id v1"


def kat_pairing_vectors():
    with open(VECTOR_PATH) as f:
        vectors = json.load(f)["vectors"]
    eq(len(vectors), 3, "pairing vector count")
    for v in vectors:
        name = v["name"]
        mobile_private = int(v["mobile_private_scalar_hex"], 16)
        device_private = int(v["device_private_scalar_hex"], 16)
        mobile_pub = unb64url(v["mobile_pub"])
        device_pub = unb64url(v["device_pub"])
        eq(mcrypto.p256_public(mobile_private), mobile_pub, name + " mobile_pub")
        eq(mcrypto.p256_public(device_private), device_pub, name + " device_pub")
        ecdh = mcrypto.p256_ecdh(device_private, mobile_pub)
        eq(ecdh, unhex(v["ecdh_secret_hex"]), name + " ecdh device side")
        eq(mcrypto.p256_ecdh(mobile_private, device_pub), ecdh, name + " ecdh mobile side")

        transcript_hash = mcrypto.sha256(v["transcript"].encode())
        eq(transcript_hash, unb64url(v["transcript_hash"]), name + " transcript_hash")

        salt = mcrypto.sha256(
            unb64url(v["mobile_nonce"]) + unb64url(v["device_nonce"]) + transcript_hash
        )
        session_secret = mcrypto.hkdf(ecdh, salt, RECORD_LABEL, 32)
        eq(session_secret, unhex(v["session_secret_hex"]), name + " session_secret")
        mobile_tx = mcrypto.hkdf_expand(session_secret, b"mobile->device", 32)
        mobile_rx = mcrypto.hkdf_expand(session_secret, b"device->mobile", 32)
        eq(mobile_tx, unhex(v["mobile_tx_key_hex"]), name + " mobile_tx_key")
        eq(mobile_rx, unhex(v["mobile_rx_key_hex"]), name + " mobile_rx_key")
        session_id = mcrypto.sha256(SESSION_ID_LABEL + transcript_hash + ecdh)[:16]
        eq(session_id, unb64url(v["session_id"]), name + " session_id")

        nonce = bytes(4) + (0).to_bytes(8, "big")
        aad = v["client_finished_aad"].encode()
        plaintext = v["client_finished_plaintext"].encode()
        sealed = unb64url(v["client_finished_ciphertext"]) + unb64url(v["client_finished_tag"])
        cipher = mcrypto.AESGCM(mobile_tx)
        eq(cipher.encrypt(nonce, plaintext, aad), sealed, name + " client_finished seal")
        eq(cipher.decrypt(nonce, sealed, aad), plaintext, name + " client_finished open")
        raises(mcrypto.InvalidTag,
               lambda: mcrypto.AESGCM(mobile_rx).decrypt(nonce, sealed, aad),
               name + " wrong direction key")


CHECKS = (
    kat_sha256,
    kat_hmac,
    kat_hkdf,
    kat_x25519_scalarmult,
    kat_x25519_iterated,
    kat_x25519_dh,
    kat_x25519_generate,
    kat_x25519_negative,
    kat_p256_ecdh,
    kat_p256_generate,
    kat_p256_negative,
    kat_gcm,
    kat_gcm_multi_chunk,
    kat_gcm_negative,
    kat_yield_hook,
    kat_pairing_vectors,
)


def main():
    for check in CHECKS:
        check()
    print("PASS %d" % len(CHECKS))


if __name__ == "__main__":
    main()
