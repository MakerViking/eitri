# SPDX-License-Identifier: Apache-2.0
"""Ed25519 signature verification (RFC 8032) in pure Python.

Only verification is implemented - the client never holds a signing key.  It is
slow compared with libsodium (tens of milliseconds) but removes any dependency
on the stock printer image, which has no minisign and may have no usable
`cryptography` wheel.  Behaviour follows libsodium's crypto_sign_verify_detached,
which is what minisign uses: S must be canonical, A must not be a small-order
point, and the check is the strict cofactorless R' == R comparison.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Tuple

P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)

Point = Tuple[int, int, int, int]  # extended homogeneous (X, Y, Z, T)


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= P:
        return None
    x2 = ((y * y - 1) * _inv(D * y * y + 1)) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = (x * SQRT_M1) % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = (4 * _inv(5)) % P
_GX = _recover_x(_GY, 0)
assert _GX is not None
G: Point = (_GX, _GY, 1, (_GX * _GY) % P)
IDENTITY: Point = (0, 1, 1, 0)


def _add(a: Point, b: Point) -> Point:
    A = ((a[1] - a[0]) * (b[1] - b[0])) % P
    B = ((a[1] + a[0]) * (b[1] + b[0])) % P
    C = (2 * a[3] * b[3] * D) % P
    Dd = (2 * a[2] * b[2]) % P
    E, F, Gg, H = B - A, Dd - C, Dd + C, B + A
    return ((E * F) % P, (Gg * H) % P, (F * Gg) % P, (E * H) % P)


def _mul(s: int, pt: Point) -> Point:
    result = IDENTITY
    addend = pt
    while s:
        if s & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        s >>= 1
    return result


def _equal(a: Point, b: Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _encode(pt: Point) -> bytes:
    zi = _inv(pt[2])
    x, y = (pt[0] * zi) % P, (pt[1] * zi) % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode(data: bytes) -> Optional[Point]:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, (x * y) % P)


def _small_order(pt: Point) -> bool:
    return _equal(_mul(8, pt), IDENTITY)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True iff *signature* is a valid Ed25519 signature of *message* by *public_key*."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_pt = _decode(public_key)
    if a_pt is None or _small_order(a_pt):
        return False
    r_bytes, s_bytes = signature[:32], signature[32:]
    s = int.from_bytes(s_bytes, "little")
    if s >= Q:
        return False
    h = int.from_bytes(hashlib.sha512(r_bytes + public_key + message).digest(), "little") % Q
    # R' = [s]B - [h]A, compared as encoded bytes
    neg_a: Point = ((-a_pt[0]) % P, a_pt[1], a_pt[2], (-a_pt[3]) % P)
    r_check = _add(_mul(s, G), _mul(h, neg_a))
    return _encode(r_check) == r_bytes
