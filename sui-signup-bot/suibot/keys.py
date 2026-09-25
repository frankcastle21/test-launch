"""Sui Ed25519 keys: generate/import, address derivation, personal-message signing,
and passphrase-based encryption of private keys at rest.

Formats follow Sui:
  address    = 0x + blake2b-256(0x00 || pubkey)
  privkey    = bech32("suiprivkey", 0x00 || 32-byte seed)
  signature  = base64(0x00 || ed25519_sig(64) || pubkey(32))
  signed msg = blake2b-256([3, 0, 0] || bcs(vector<u8> message))   (PersonalMessage intent)
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ED25519_FLAG = 0x00
PERSONAL_MESSAGE_INTENT = bytes([3, 0, 0])
BECH32_HRP = "suiprivkey"


class KeyError_(ValueError):
    pass


# --- bech32 (BIP-173), used by Sui for "suiprivkey1..." strings ----------------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits, tobits, pad):
    acc, bits, ret, maxv = 0, 0, [], (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        raise KeyError_("Invalid bech32 padding")
    return ret


def bech32_encode(hrp: str, payload: bytes) -> str:
    data = _convertbits(payload, 8, 5, True)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def bech32_decode(s: str) -> tuple[str, bytes]:
    s = s.strip().lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise KeyError_("Not a bech32 string")
    hrp, data = s[:pos], []
    for c in s[pos + 1:]:
        if c not in _CHARSET:
            raise KeyError_("Invalid bech32 character")
        data.append(_CHARSET.index(c))
    if _polymod(_hrp_expand(hrp) + data) != 1:
        raise KeyError_("Bad bech32 checksum")
    return hrp, bytes(_convertbits(data[:-6], 5, 8, False))


# --- keys, addresses, signatures ---------------------------------------------
def pubkey_of(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)


def address_of(pubkey: bytes) -> str:
    return "0x" + hashlib.blake2b(bytes([ED25519_FLAG]) + pubkey, digest_size=32).hexdigest()


def generate_seed() -> bytes:
    return os.urandom(32)


def encode_suiprivkey(seed: bytes) -> str:
    return bech32_encode(BECH32_HRP, bytes([ED25519_FLAG]) + seed)


def parse_private_key(text: str) -> bytes:
    """Accepts `suiprivkey1...` (Ed25519 only) or a 64-char hex seed."""
    text = text.strip()
    if text.lower().startswith(BECH32_HRP + "1"):
        hrp, payload = bech32_decode(text)
        if hrp != BECH32_HRP or len(payload) != 33:
            raise KeyError_("Malformed suiprivkey")
        if payload[0] != ED25519_FLAG:
            raise KeyError_("Only Ed25519 keys are supported (not secp256k1/secp256r1)")
        return payload[1:]
    hex_part = text[2:] if text.startswith("0x") else text
    try:
        seed = bytes.fromhex(hex_part)
    except ValueError:
        raise KeyError_("Key is neither suiprivkey1... nor hex")
    if len(seed) != 32:
        raise KeyError_("Hex key must be 32 bytes")
    return seed


def _uleb128(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def personal_message_digest(message: bytes) -> bytes:
    bcs = _uleb128(len(message)) + message
    return hashlib.blake2b(PERSONAL_MESSAGE_INTENT + bcs, digest_size=32).digest()


def sign_personal_message(seed: bytes, message: bytes) -> str:
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(personal_message_digest(message))
    return base64.b64encode(bytes([ED25519_FLAG]) + sig + pubkey_of(seed)).decode()


def verify_personal_message(address: str, message: bytes, signature_b64: str) -> bool:
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if len(raw) != 97 or raw[0] != ED25519_FLAG:
        return False
    sig, pub = raw[1:65], raw[65:]
    if address_of(pub) != address.lower():
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, personal_message_digest(message))
        return True
    except InvalidSignature:
        return False


# --- encryption at rest -----------------------------------------------------
class WrongPassphrase(Exception):
    pass


def derive_fernet(passphrase: str, salt: bytes) -> Fernet:
    if not passphrase:
        raise WrongPassphrase("Empty passphrase")
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())
    return Fernet(base64.urlsafe_b64encode(key))


@dataclass(frozen=True)
class Wallet:
    """A decrypted wallet. Only exists in memory during an attempt."""
    label: str
    address: str
    seed: bytes

    def __repr__(self) -> str:
        return f"Wallet({self.label!r}, {self.address!r})"

    @property
    def public_key(self) -> bytes:
        return pubkey_of(self.seed)

    def sign_personal_message(self, message: bytes) -> str:
        return sign_personal_message(self.seed, message)


def decrypt_seed(fernet: Fernet, token: str) -> bytes:
    try:
        return fernet.decrypt(token.encode())
    except InvalidToken:
        raise WrongPassphrase("Could not decrypt key (wrong passphrase?)")
