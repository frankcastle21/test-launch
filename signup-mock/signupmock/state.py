"""Durable run state in SQLite, so a restarted VPS never repeats completed wallets.

Every state transition is committed before the next network call, and the
global pacing gate (earliest time the next attempt may start) is persisted too,
so a restart in the middle of a 4-7 minute wait resumes the wait instead of
firing immediately.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    position        INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    outcome         TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_response   TEXT,
    updated_at      REAL NOT NULL,
    completed_at    REAL
);
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    address      TEXT NOT NULL,
    attempt_no   INTEGER NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL NOT NULL,
    result       TEXT NOT NULL,
    http_status  INTEGER,
    error        TEXT,
    response     TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class WalletRow:
    address: str
    label: str
    position: int
    status: str
    outcome: str | None
    attempts: int
    next_attempt_at: float
    last_error: str | None


def _dumps(obj) -> str | None:
    return None if obj is None else json.dumps(obj, sort_keys=True)


class StateStore:
    def __init__(self, path: str | Path, clock=time.time) -> None:
        self.path = str(path)
        self.clock = clock
        self.db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _tx(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.db:  # BEGIN ... COMMIT
            self.db.execute("BEGIN IMMEDIATE")
            return self.db.execute(sql, params)

    # Wallet queue -----------------------------------------------------------
    def sync_wallets(self, wallets) -> int:
        """Insert wallets not seen before. Existing rows (and their status) are kept."""
        now = self.clock()
        added = 0
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for pos, w in enumerate(wallets):
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO wallets (address, label, position, updated_at) "
                    "VALUES (?, ?, ?, ?)", (w.address, w.label, pos, now))
                added += cur.rowcount
                self.db.execute("UPDATE wallets SET label = ?, position = ? WHERE address = ?",
                                (w.label, pos, w.address))
        return added

    def _row(self, r: sqlite3.Row) -> WalletRow:
        return WalletRow(r["address"], r["label"], r["position"], r["status"], r["outcome"],
                         r["attempts"], r["next_attempt_at"], r["last_error"])

    def get(self, address: str) -> WalletRow | None:
        r = self.db.execute("SELECT * FROM wallets WHERE address = ?", (address,)).fetchone()
        return self._row(r) if r else None

    def pending(self, addresses: set[str] | None = None) -> list[WalletRow]:
        rows = self.db.execute(
            "SELECT * FROM wallets WHERE status = ? ORDER BY next_attempt_at, position",
            (PENDING,)).fetchall()
        out = [self._row(r) for r in rows]
        return [r for r in out if addresses is None or r.address in addresses]

    def recover_in_progress(self) -> int:
        """Wallets interrupted mid-attempt go back to pending.

        Safe because the client checks GET /api/signup before any POST, so an
        attempt whose response was lost is detected as already registered.
        """
        return self._tx("UPDATE wallets SET status = ?, updated_at = ? WHERE status = ?",
                        (PENDING, self.clock(), IN_PROGRESS)).rowcount

    def mark_in_progress(self, address: str) -> int:
        """Returns the attempt number now starting."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE wallets SET status = ?, attempts = attempts + 1, updated_at = ? "
                "WHERE address = ?", (IN_PROGRESS, self.clock(), address))
            return self.db.execute("SELECT attempts FROM wallets WHERE address = ?",
                                   (address,)).fetchone()[0]

    def mark_done(self, address: str, outcome: str, response) -> None:
        now = self.clock()
        self._tx("UPDATE wallets SET status = ?, outcome = ?, last_error = NULL, "
                 "last_response = ?, updated_at = ?, completed_at = ? WHERE address = ?",
                 (DONE, outcome, _dumps(response), now, now, address))

    def mark_retry(self, address: str, next_attempt_at: float, error: str, response) -> None:
        self._tx("UPDATE wallets SET status = ?, next_attempt_at = ?, last_error = ?, "
                 "last_response = ?, updated_at = ? WHERE address = ?",
                 (PENDING, next_attempt_at, error, _dumps(response), self.clock(), address))

    def mark_failed(self, address: str, error: str, response) -> None:
        now = self.clock()
        self._tx("UPDATE wallets SET status = ?, last_error = ?, last_response = ?, "
                 "updated_at = ?, completed_at = ? WHERE address = ?",
                 (FAILED, error, _dumps(response), now, now, address))

    def reset_failed(self) -> int:
        return self._tx("UPDATE wallets SET status = ?, attempts = 0, next_attempt_at = 0, "
                        "completed_at = NULL, updated_at = ? WHERE status = ?",
                        (PENDING, self.clock(), FAILED)).rowcount

    def record_attempt(self, address: str, attempt_no: int, started_at: float, result: str,
                       http_status: int | None, error: str | None, response) -> None:
        self._tx("INSERT INTO attempts (address, attempt_no, started_at, finished_at, result, "
                 "http_status, error, response) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (address, attempt_no, started_at, self.clock(), result, http_status, error,
                  _dumps(response)))

    # Pacing gate ------------------------------------------------------------
    def next_run_at(self) -> float:
        r = self.db.execute("SELECT value FROM meta WHERE key = 'next_run_at'").fetchone()
        return float(r[0]) if r else 0.0

    def set_next_run_at(self, ts: float) -> None:
        self._tx("INSERT INTO meta (key, value) VALUES ('next_run_at', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (repr(ts),))

    # Reporting --------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM wallets GROUP BY status").fetchall()
        out = {PENDING: 0, IN_PROGRESS: 0, DONE: 0, FAILED: 0}
        out.update({r[0]: r[1] for r in rows})
        return out

    def rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wallets ORDER BY position").fetchall()

    def attempts_for(self, address: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM attempts WHERE address = ? ORDER BY id",
                               (address,)).fetchall()
