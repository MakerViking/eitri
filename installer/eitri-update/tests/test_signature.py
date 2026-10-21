# SPDX-License-Identifier: Apache-2.0
import os
import shutil
import subprocess

import pytest

from eitri_update import ed25519, minisign

# RFC 8032 section 7.1 test vectors (secret key not needed for verification)
RFC8032 = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a0a38da47eb0d96ddab2b6bf4a8cc2b7a2d1e3b7c9ba8e8f9ad8", "af82",
     None),
]


@pytest.mark.parametrize("pub,msg,sig", [v for v in RFC8032 if v[2]])
def test_rfc8032_vectors(pub, msg, sig):
    assert ed25519.verify(bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig))
    bad = bytearray(bytes.fromhex(sig))
    bad[10] ^= 1
    assert not ed25519.verify(bytes.fromhex(pub), bytes.fromhex(msg), bytes(bad))
    assert not ed25519.verify(bytes.fromhex(pub), bytes.fromhex(msg) + b"x", bytes.fromhex(sig))


def test_rejects_noncanonical_s_and_small_order_keys():
    pub, msg, sig = [bytes.fromhex(x) for x in RFC8032[0]]
    s = int.from_bytes(sig[32:], "little") + ed25519.Q
    assert not ed25519.verify(pub, msg, sig[:32] + int.to_bytes(s, 32, "little"))   # malleated S
    identity = int.to_bytes(1, 32, "little")
    assert not ed25519.verify(identity, msg, sig)                                   # small-order public key
    assert not ed25519.verify(pub[:31], msg, sig)
    assert not ed25519.verify(pub, msg, sig[:63])


def test_cross_check_against_cryptography_library():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization

    for i in range(5):
        sk = crypto.Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        msg = os.urandom(i * 37)
        assert ed25519.verify(pk, msg, sk.sign(msg))


needs_minisign = pytest.mark.skipif(shutil.which("minisign") is None, reason="minisign binary not installed")


def _keypair(tmp_path, name="k"):
    pub, sec = str(tmp_path / (name + ".pub")), str(tmp_path / (name + ".key"))
    subprocess.run(["minisign", "-G", "-W", "-f", "-p", pub, "-s", sec], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return pub, sec


def _sign(tmp_path, sec, data, legacy=False, comment="release 2026.10.0"):
    f = tmp_path / "manifest.json"
    f.write_bytes(data)
    cmd = ["minisign", "-S", "-s", sec, "-m", str(f), "-x", str(f) + ".minisig", "-t", comment]
    if legacy:
        cmd.append("-l")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(f)


@needs_minisign
@pytest.mark.parametrize("legacy", [False, True])
def test_verifies_real_minisign_output(tmp_path, legacy):
    pub, sec = _keypair(tmp_path)
    f = _sign(tmp_path, sec, b'{"schema":1}\n' * 1000, legacy=legacy)
    sig = minisign.verify_file(f, None, [pub])
    assert sig.trusted_comment == "release 2026.10.0"
    assert sig.algorithm == (b"Ed" if legacy else b"ED")


@needs_minisign
def test_tampering_is_detected(tmp_path):
    pub, sec = _keypair(tmp_path)
    f = _sign(tmp_path, sec, b"original")
    with open(f, "wb") as fh:
        fh.write(b"0riginal")
    with pytest.raises(minisign.SignatureError, match="does not match"):
        minisign.verify_file(f, None, [pub])
    # trusted comment edited -> global signature fails
    f = _sign(tmp_path, sec, b"original")
    sigfile = f + ".minisig"
    text = open(sigfile).read().replace("release 2026.10.0", "release 9999.99.9")
    open(sigfile, "w").write(text)
    with pytest.raises(minisign.SignatureError, match="trusted comment"):
        minisign.verify_file(f, None, [pub])


@needs_minisign
def test_unknown_key_and_key_rotation(tmp_path):
    pub1, sec1 = _keypair(tmp_path, "one")
    pub2, sec2 = _keypair(tmp_path, "two")
    f = _sign(tmp_path, sec2, b"data")
    with pytest.raises(minisign.SignatureError, match="unknown key"):
        minisign.verify_file(f, None, [pub1])
    minisign.verify_file(f, None, [pub1, pub2])               # either key in the ring is accepted
    keydir = tmp_path / "keys"
    keydir.mkdir()
    shutil.copy(pub1, keydir / "a.pub")
    shutil.copy(pub2, keydir / "b.pub")
    minisign.verify_file(f, None, [str(keydir)])


@needs_minisign
def test_cli_exit_codes(tmp_path):
    pub, sec = _keypair(tmp_path)
    f = _sign(tmp_path, sec, b"abc")
    assert minisign.main(["verify", f, "--pubkey", pub]) == 0
    open(f, "wb").write(b"abd")
    assert minisign.main(["verify", f, "--pubkey", pub]) == 1


def test_malformed_inputs():
    with pytest.raises(minisign.SignatureError):
        minisign.parse_signature("garbage")
    with pytest.raises(minisign.SignatureError):
        minisign.parse_public_key("untrusted comment: x\n!!!notbase64!!!")
    with pytest.raises(minisign.SignatureError):
        minisign.load_keys([])
