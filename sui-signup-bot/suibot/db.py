"""SQLite store: encrypted wallets, the signup queue, attempt history, pacing gate."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from . import keys

PENDING, IN_PROGRESS, DONE, FAILED, NEEDS_HUMAN = (
    "pending", "in_progress", "done", "failed", "needs_human")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id              INTEGER PRIMARY KEY,
    address         TEXT NOT NULL UNIQUE,
    label           TEXT NOT NULL,
    enc_key         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    completed_at    REAL,
    result          TEXT,
    error           TEXT,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY,
    address     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL NOT NULL,
    success     INTEGER NOT NULL,
    result      TEXT,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path, clock=time.time) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        new = not Path(path).exists()
        self.db = sqlite3.connect(str(path), isolation_level=None)
        if new:
            os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.clock = clock
        self._fernet = None

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self.db.execute(sql, params)

    # meta ----------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                   "excluded.value", (key, value))

    # keystore --------------------------------------------------------------
    def unlock(self, passphrase: str) -> None:
        """Derive the encryption key. The first unlock sets the passphrase for this DB."""
        salt_hex = self.get_meta("kdf_salt")
        if salt_hex is None:
            salt_hex = os.urandom(16).hex()
            self.set_meta("kdf_salt", salt_hex)
        fernet = keys.derive_fernet(passphrase, bytes.fromhex(salt_hex))
        check = self.get_meta("kdf_check")
        if check is None:
            self.set_meta("kdf_check", fernet.encrypt(b"suibot").decode())
        elif keys.decrypt_seed(fernet, check) != b"suibot":
            raise keys.WrongPassphrase("Wrong passphrase")
        self._fernet = fernet

    def add_wallet(self, seed: bytes, label: str) -> str | None:
        """Returns the address, or None if it was already stored."""
        if self._fernet is None:
            raise RuntimeError("Store is locked; call unlock() first")
        address = keys.address_of(keys.pubkey_of(seed))
        cur = self._exec(
            "INSERT OR IGNORE INTO wallets (address, label, enc_key, created_at) "
            "VALUES (?, ?, ?, ?)",
            (address, label, self._fernet.encrypt(seed).decode(), self.clock()))
        return address if cur.rowcount else None

    def load_wallet(self, address: str) -> keys.Wallet:
        row = self.db.execute("SELECT label, enc_key FROM wallets WHERE address = ?",
                              (address,)).fetchone()
        seed = keys.decrypt_seed(self._fernet, row["enc_key"])
        if keys.address_of(keys.pubkey_of(seed)) != address:
            raise keys.WrongPassphrase(f"Stored key does not match {address}")
        return keys.Wallet(row["label"], address, seed)

    def wallet_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]

    # queue -----------------------------------------------------------------
    def recover_in_progress(self) -> int:
        """Re-queue wallets whose attempt was cut off (crash, kill, power loss) and log it."""
        now = self.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO attempts (address, started_at, finished_at, success, result, error) "
                "SELECT address, COALESCE(last_attempt_at, ?), ?, 0, 'interrupted', "
                "'process stopped mid-attempt; re-queued' FROM wallets WHERE status = ?",
                (now, now, IN_PROGRESS))
            return self.db.execute("UPDATE wallets SET status = ? WHERE status = ?",
                                   (PENDING, IN_PROGRESS)).rowcount

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wallets WHERE status = ? "
                               "ORDER BY next_attempt_at, id", (PENDING,)).fetchall()

    def start_attempt(self, address: str) -> int:
        now = self.clock()
        self._exec("UPDATE wallets SET status = ?, attempts = attempts + 1, "
                   "last_attempt_at = ? WHERE address = ?", (IN_PROGRESS, now, address))
        return self.db.execute("SELECT attempts FROM wallets WHERE address = ?",
                               (address,)).fetchone()[0]

    def finish_attempt(self, address: str, started_at: float, *, status: str,
                       success: bool, result: str | None = None, error: str | None = None,
                       next_attempt_at: float = 0.0) -> None:
        now = self.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE wallets SET status = ?, result = ?, error = ?, next_attempt_at = ?, "
                "completed_at = ? WHERE address = ?",
                (status, result, error, next_attempt_at,
                 now if status in (DONE, FAILED, NEEDS_HUMAN) else None, address))
            self.db.execute(
                "INSERT INTO attempts (address, started_at, finished_at, success, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (address, started_at, now, int(success), result, error))

    def reset(self, statuses=(FAILED, NEEDS_HUMAN)) -> int:
        marks = ",".join("?" * len(statuses))
        return self._exec(f"UPDATE wallets SET status = ?, attempts = 0, next_attempt_at = 0, "
                          f"completed_at = NULL WHERE status IN ({marks})",
                          (PENDING, *statuses)).rowcount

    def next_run_at(self) -> float:
        return float(self.get_meta("next_run_at") or 0)

    def set_next_run_at(self, ts: float) -> None:
        self.set_meta("next_run_at", repr(ts))

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in (PENDING, IN_PROGRESS, DONE, FAILED, NEEDS_HUMAN)}
        for status, n in self.db.execute("SELECT status, COUNT(*) FROM wallets GROUP BY status"):
            out[status] = n
        return out

    def all_wallets(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wallets ORDER BY id").fetchall()

    def attempts(self, address: str | None = None) -> list[sqlite3.Row]:
        if address:
            return self.db.execute("SELECT * FROM attempts WHERE address = ? ORDER BY id",
                                   (address,)).fetchall()
        return self.db.execute("SELECT * FROM attempts ORDER BY id").fetchall()
