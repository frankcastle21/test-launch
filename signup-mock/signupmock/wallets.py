"""Local wallet list: generation, encryption at rest, loading and signing.

Wallets are Ed25519 keypairs. Addresses are derived the way Sui derives them
(blake2b-256 over the scheme flag byte 0x00 followed by the public key), and
signatures are serialized Sui-style as base64(flag || signature || pubkey).
The mock server only ever sees signatures, never secret keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ED25519_FLAG = 0x00
PLAIN_FORMAT = "signupmock-wallets-v1"
ENCRYPTED_FORMAT = "signupmock-wallets-enc-v1"
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


class WalletFileError(Exception):
    pass


def address_from_pubkey(pubkey: bytes) -> str:
    digest = hashlib.blake2b(bytes([ED25519_FLAG]) + pubkey, digest_size=32).hexdigest()
    return "0x" + digest


def is_valid_address(address: str) -> bool:
    if not isinstance(address, str) or not address.startswith("0x") or len(address) != 66:
        return False
    try:
        int(address[2:], 16)
    except ValueError:
        return False
    return address == address.lower()


@dataclass(frozen=True)
class Wallet:
    label: str
    address: str
    secret_key: bytes  # raw 32-byte Ed25519 seed

    def __repr__(self) -> str:  # never leak key material into logs/tracebacks
        return f"Wallet(label={self.label!r}, address={self.address!r})"

    def _private_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.secret_key)

    @property
    def public_key(self) -> bytes:
        return self._private_key().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message: bytes) -> str:
        sig = self._private_key().sign(message)
        return base64.b64encode(bytes([ED25519_FLAG]) + sig + self.public_key).decode()


def verify_signature(address: str, message: bytes, serialized: str) -> bool:
    """Server-side check: signature is valid and the embedded key owns `address`."""
    try:
        raw = base64.b64decode(serialized, validate=True)
    except (ValueError, TypeError):
        return False
    if len(raw) != 1 + 64 + 32 or raw[0] != ED25519_FLAG:
        return False
    sig, pub = raw[1:65], raw[65:]
    if address_from_pubkey(pub) != address:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
    except InvalidSignature:
        return False
    return True


def generate(count: int, label_prefix: str = "wallet") -> list[Wallet]:
    wallets = []
    for i in range(count):
        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes_raw()
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        wallets.append(Wallet(f"{label_prefix}-{i + 1:03d}", address_from_pubkey(pub), seed))
    return wallets


def to_plain_json(wallets: list[Wallet]) -> bytes:
    doc = {
        "format": PLAIN_FORMAT,
        "wallets": [
            {"label": w.label, "address": w.address, "secret_key": w.secret_key.hex()}
            for w in wallets
        ],
    }
    return json.dumps(doc, indent=2).encode()


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    raw = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    if not passphrase:
        raise WalletFileError("An empty passphrase is not allowed")
    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(plaintext)
    doc = {
        "format": ENCRYPTED_FORMAT,
        "kdf": {"name": "scrypt", "salt": base64.b64encode(salt).decode(),
                "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
        "ciphertext": token.decode(),
    }
    return json.dumps(doc, indent=2).encode()


def decrypt(blob: bytes, passphrase: str) -> bytes:
    doc = json.loads(blob)
    kdf = doc["kdf"]
    key = _derive_key(passphrase, base64.b64decode(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
    try:
        return Fernet(key).decrypt(doc["ciphertext"].encode())
    except InvalidToken:
        raise WalletFileError("Could not decrypt wallet file (wrong passphrase or corrupted file)")


def is_encrypted(path: Path) -> bool:
    try:
        return json.loads(path.read_bytes()).get("format") == ENCRYPTED_FORMAT
    except (ValueError, AttributeError):
        return False


def parse_plain(blob: bytes) -> list[Wallet]:
    try:
        doc = json.loads(blob)
    except ValueError as e:
        raise WalletFileError(f"Wallet file is not valid JSON: {e}")
    if doc.get("format") != PLAIN_FORMAT:
        raise WalletFileError(f"Unexpected wallet file format: {doc.get('format')!r}")
    wallets: list[Wallet] = []
    seen: set[str] = set()
    for i, entry in enumerate(doc.get("wallets", [])):
        try:
            seed = bytes.fromhex(entry["secret_key"])
            label = str(entry.get("label") or f"wallet-{i + 1:03d}")
        except (KeyError, ValueError) as e:
            raise WalletFileError(f"Wallet entry {i} is malformed: {e}")
        if len(seed) != 32:
            raise WalletFileError(f"Wallet entry {i} ({label}): secret_key must be 32 bytes")
        pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)
        address = address_from_pubkey(pub)
        if entry.get("address") and entry["address"].lower() != address:
            raise WalletFileError(f"Wallet entry {i} ({label}): address does not match its key")
        if address in seen:
            raise WalletFileError(f"Wallet entry {i} ({label}): duplicate address {address}")
        seen.add(address)
        wallets.append(Wallet(label, address, seed))
    if not wallets:
        raise WalletFileError("Wallet file contains no wallets")
    return wallets


def load(path: str | Path, passphrase: str | None = None) -> list[Wallet]:
    path = Path(path)
    blob = path.read_bytes()
    if is_encrypted(path):
        if passphrase is None:
            raise WalletFileError("Wallet file is encrypted; a passphrase is required")
        return parse_plain(decrypt(blob, passphrase))
    return parse_plain(blob)


def plain_file_is_exposed(path: str | Path) -> bool:
    """True if an unencrypted wallet file is readable by group/other."""
    mode = Path(path).stat().st_mode
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


def write_private(path: str | Path, data: bytes) -> None:
    """Write a file readable only by the owner (0600), atomically."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
