import hashlib
import hmac as std_hmac
import os
import random
import sys

import pytest
from cryptography.exceptions import InvalidTag as RefInvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM as RefAESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kat_mcrypto
from kat_mcrypto import mcrypto

GCM_SIZES = (0, 1, 15, 16, 17, 31, 32, 33, 63, 64, 255, 1023, 1024, 1025, 2049, 4096)
AAD_SIZES = (0, 1, 15, 16, 17, 20, 64, 1024, 1500)


def p256_ref_key(scalar):
    return ec.derive_private_key(scalar, ec.SECP256R1())


def p256_ref_public(key):
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )


def x25519_ref_public(private):
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )


@pytest.mark.parametrize("check", kat_mcrypto.CHECKS, ids=lambda c: c.__name__)
def test_known_answers(check):
    check()


def test_kat_gcm_vectors_agree_with_reference():
    for key, iv, plaintext, aad, ciphertext, tag in kat_mcrypto.GCM_VECTORS:
        sealed = RefAESGCM(bytes.fromhex(key)).encrypt(
            bytes.fromhex(iv), bytes.fromhex(plaintext), bytes.fromhex(aad),
        )
        assert sealed.hex() == ciphertext + tag


def test_random_bytes():
    assert mcrypto.random_bytes(0) == b""
    assert len(mcrypto.random_bytes(48)) == 48
    assert mcrypto.random_bytes(32) != mcrypto.random_bytes(32)


def test_sha256_matches_hashlib():
    for size in (0, 1, 55, 56, 63, 64, 65, 1000):
        data = os.urandom(size)
        assert mcrypto.sha256(data) == hashlib.sha256(data).digest()


def test_hmac_matches_stdlib():
    rng = random.Random(1)
    for key_size in (0, 1, 20, 32, 63, 64, 65, 131, 200):
        for _ in range(20):
            key = os.urandom(key_size)
            msg = os.urandom(rng.randrange(0, 300))
            assert mcrypto.hmac_sha256(key, msg) == std_hmac.new(key, msg, hashlib.sha256).digest()


def test_hkdf_matches_reference():
    rng = random.Random(2)
    for _ in range(200):
        ikm = os.urandom(rng.randrange(1, 80))
        salt = os.urandom(rng.choice((0, 1, 16, 32, 64, 100)))
        info = os.urandom(rng.choice((0, 1, 14, 23, 80)))
        length = rng.choice((1, 16, 31, 32, 33, 64, 82, 255, 1000, 255 * 32))
        expected = HKDF(hashes.SHA256(), length, salt or None, info).derive(ikm)
        assert mcrypto.hkdf(ikm, salt, info, length) == expected
        prk = mcrypto.hkdf_extract(salt, ikm)
        assert prk == std_hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()
        assert mcrypto.hkdf_expand(prk, info, length) == HKDFExpand(
            hashes.SHA256(), length, info,
        ).derive(prk)


def test_noise_hkdf_chain_matches_reference():
    for _ in range(50):
        chaining_key, ikm = os.urandom(32), os.urandom(32)
        temp = std_hmac.new(chaining_key, ikm, hashlib.sha256).digest()
        out1 = std_hmac.new(temp, b"\x01", hashlib.sha256).digest()
        out2 = std_hmac.new(temp, out1 + b"\x02", hashlib.sha256).digest()
        assert mcrypto.hkdf(ikm, chaining_key, b"", 64) == out1 + out2


def test_x25519_matches_reference():
    for _ in range(200):
        private, peer_private = os.urandom(32), os.urandom(32)
        peer_public = x25519_ref_public(peer_private)
        assert mcrypto.x25519_public(private) == x25519_ref_public(private)
        expected = X25519PrivateKey.from_private_bytes(private).exchange(
            X25519PublicKey.from_public_bytes(peer_public),
        )
        assert mcrypto.x25519(private, peer_public) == expected


def test_x25519_arbitrary_u_matches_reference():
    done = 0
    while done < 200:
        private, u = os.urandom(32), os.urandom(32)
        try:
            expected = X25519PrivateKey.from_private_bytes(private).exchange(
                X25519PublicKey.from_public_bytes(u),
            )
        except ValueError:
            assert mcrypto.x25519(private, u) == bytes(32)
            continue
        assert mcrypto.x25519(private, u) == expected
        done += 1


def test_x25519_generate_interoperates():
    for _ in range(20):
        private, public = mcrypto.x25519_generate()
        assert public == x25519_ref_public(private)
        peer = X25519PrivateKey.generate()
        peer_public = peer.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        assert mcrypto.x25519(private, peer_public) == peer.exchange(
            X25519PublicKey.from_public_bytes(public),
        )


def test_p256_matches_reference():
    rng = random.Random(3)
    scalars = [1, 2, 3, kat_mcrypto.P256_N - 1, kat_mcrypto.P256_N - 2, 1 << 255, (1 << 128) - 1]
    scalars += [rng.randrange(1, kat_mcrypto.P256_N) for _ in range(150)]
    scalars += [rng.randrange(1, 1 << bits) for bits in range(1, 64)]
    for scalar in scalars:
        ref = p256_ref_key(scalar)
        assert mcrypto.p256_public(scalar) == p256_ref_public(ref)
        peer = p256_ref_key(rng.randrange(1, kat_mcrypto.P256_N))
        expected = ref.exchange(ec.ECDH(), peer.public_key())
        assert mcrypto.p256_ecdh(scalar, p256_ref_public(peer)) == expected


def test_p256_generate_interoperates():
    for _ in range(20):
        private, public = mcrypto.p256_generate()
        assert 0 < private < kat_mcrypto.P256_N
        assert public == p256_ref_public(p256_ref_key(private))
        peer = ec.generate_private_key(ec.SECP256R1())
        ours = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        assert mcrypto.p256_ecdh(private, p256_ref_public(peer)) == peer.exchange(ec.ECDH(), ours)


def test_p256_rejects_what_reference_rejects():
    rng = random.Random(4)
    rejected = 0
    for _ in range(300):
        point = bytearray(p256_ref_public(ec.generate_private_key(ec.SECP256R1())))
        point[rng.randrange(1, 65)] ^= 1 << rng.randrange(8)
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes(point))
        except ValueError:
            rejected += 1
            with pytest.raises(ValueError):
                mcrypto.p256_ecdh(5, bytes(point))
        else:
            mcrypto.p256_ecdh(5, bytes(point))
    assert rejected > 250


@pytest.mark.parametrize("key_size", (16, 32))
def test_gcm_matches_reference(key_size):
    for size in GCM_SIZES:
        for aad_size in AAD_SIZES:
            key, nonce = os.urandom(key_size), os.urandom(12)
            plaintext, aad = os.urandom(size), os.urandom(aad_size)
            ref = RefAESGCM(key)
            ours = mcrypto.AESGCM(key)
            sealed = ours.encrypt(nonce, plaintext, aad)
            assert sealed == ref.encrypt(nonce, plaintext, aad)
            assert ours.decrypt(nonce, sealed, aad) == plaintext
            assert ref.decrypt(nonce, sealed, aad) == plaintext


def test_gcm_random_lengths_match_reference():
    rng = random.Random(5)
    for _ in range(300):
        key, nonce = os.urandom(rng.choice((16, 32))), os.urandom(12)
        plaintext = os.urandom(rng.randrange(0, 3000))
        aad = rng.choice((None, b"", os.urandom(rng.randrange(1, 200))))
        sealed = mcrypto.AESGCM(key).encrypt(nonce, plaintext, aad)
        assert sealed == RefAESGCM(key).encrypt(nonce, plaintext, aad)
        assert mcrypto.AESGCM(key).decrypt(nonce, sealed, aad) == plaintext


def test_gcm_instance_reuse_across_messages():
    key = os.urandom(32)
    ours, ref = mcrypto.AESGCM(key), RefAESGCM(key)
    for counter in range(50):
        nonce = bytes(4) + counter.to_bytes(8, "big")
        plaintext = os.urandom(counter * 37)
        assert ours.encrypt(nonce, plaintext, b"ad") == ref.encrypt(nonce, plaintext, b"ad")


def test_gcm_accepts_bytearray_and_memoryview():
    key, nonce = os.urandom(32), os.urandom(12)
    plaintext, aad = os.urandom(100), os.urandom(20)
    expected = RefAESGCM(key).encrypt(nonce, plaintext, aad)
    ours = mcrypto.AESGCM(bytearray(key))
    assert ours.encrypt(bytearray(nonce), memoryview(plaintext), bytearray(aad)) == expected
    assert ours.decrypt(memoryview(nonce), bytearray(expected), memoryview(aad)) == plaintext


@pytest.mark.parametrize("size", (0, 1, 16, 17, 1025))
def test_gcm_every_tampered_bit_is_rejected(size):
    rng = random.Random(size)
    key, nonce, aad = os.urandom(32), os.urandom(12), os.urandom(13)
    ours = mcrypto.AESGCM(key)
    sealed = ours.encrypt(nonce, os.urandom(size), aad)
    positions = range(len(sealed)) if len(sealed) <= 64 else rng.sample(range(len(sealed)), 64)
    for position in positions:
        tampered = bytearray(sealed)
        tampered[position] ^= 1 << rng.randrange(8)
        with pytest.raises(mcrypto.InvalidTag):
            ours.decrypt(nonce, bytes(tampered), aad)
        with pytest.raises(RefInvalidTag):
            RefAESGCM(key).decrypt(nonce, bytes(tampered), aad)
    for bad_aad in (None, b"", aad + b"\x00", aad[:-1], bytes(13)):
        with pytest.raises(mcrypto.InvalidTag):
            ours.decrypt(nonce, sealed, bad_aad)
    for cut in (1, 15, 16, len(sealed)):
        with pytest.raises(mcrypto.InvalidTag):
            ours.decrypt(nonce, sealed[:-cut], aad)


def test_decrypts_reference_ciphertext_for_noise_style_nonces():
    key = os.urandom(32)
    for counter in (0, 1, 255, 1 << 32, (1 << 64) - 1):
        nonce = bytes(4) + counter.to_bytes(8, "big")
        sealed = RefAESGCM(key).encrypt(nonce, b"payload", b"handshake hash")
        assert mcrypto.AESGCM(key).decrypt(nonce, sealed, b"handshake hash") == b"payload"
