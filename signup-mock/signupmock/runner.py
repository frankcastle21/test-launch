"""Sequential scheduler: one wallet at a time, randomized spacing, conservative retries."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .client import PERMANENT, THROTTLED, ClientError
from .state import StateStore

log = logging.getLogger("signupmock")


@dataclass
class RunConfig:
    min_delay: float = 240.0          # seconds between attempts (4 min)
    max_delay: float = 420.0          # (7 min)
    max_attempts: int = 4             # per wallet, including the first
    backoff_base: float = 600.0       # first retry of a wallet waits >= 10 min
    backoff_max: float = 3600.0
    throttle_pause: float = 1800.0    # global pause after any 429 (at least Retry-After)
    max_consecutive_failures: int = 3  # stop the run entirely after this many in a row
    time_scale: float = 1.0           # <1 compresses every wait, for local testing only
    once: bool = False                # process at most one wallet, then exit

    def validate(self) -> None:
        if not 0 < self.min_delay <= self.max_delay:
            raise ValueError("Need 0 < min_delay <= max_delay")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not 0 < self.time_scale <= 1:
            raise ValueError("time_scale must be in (0, 1]")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    """Append-only JSONL audit log. Never receives tokens or key material."""

    def __init__(self, path: str | Path | None, clock=time.time) -> None:
        self.path = Path(path) if path else None
        self.clock = clock

    def emit(self, event: str, **fields) -> None:
        record = {"ts": iso(self.clock()), "event": event, **fields}
        log.info("%s %s", event, json.dumps(fields, sort_keys=True, default=str))
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("[redacted]" if k.lower() in {"token", "authorization", "signature"}
                    else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class Runner:
    def __init__(self, config: RunConfig, store: StateStore, wallets, client,
                 events: EventLog | None = None, clock=time.time, sleep=None,
                 rng: random.Random | None = None) -> None:
        config.validate()
        self.cfg = config
        self.store = store
        self.wallets = {w.address: w for w in wallets}
        self.order = [w.address for w in wallets]
        self.client = client
        self.events = events or EventLog(None, clock)
        self.clock = clock
        self.stop_event = threading.Event()
        self._sleep = sleep or self.stop_event.wait
        self.rng = rng or random.SystemRandom()
        self.consecutive_failures = 0

    def stop(self) -> None:
        self.stop_event.set()

    def _scaled(self, seconds: float) -> float:
        return seconds * self.cfg.time_scale

    def _spacing(self) -> float:
        return self._scaled(self.rng.uniform(self.cfg.min_delay, self.cfg.max_delay))

    def _backoff(self, attempt_no: int, retry_after: float | None) -> float:
        base = min(self.cfg.backoff_max, self.cfg.backoff_base * 2 ** (attempt_no - 1))
        delay = base * self.rng.uniform(0.8, 1.2)
        return self._scaled(max(delay, retry_after or 0.0))

    def _wait_until(self, ts: float, reason: str) -> None:
        remaining = ts - self.clock()
        if remaining <= 0:
            return
        self.events.emit("waiting", until=iso(ts), seconds=round(remaining, 1), reason=reason)
        self._sleep(remaining)

    # Main loop --------------------------------------------------------------
    def run(self) -> dict:
        added = self.store.sync_wallets([self.wallets[a] for a in self.order])
        recovered = self.store.recover_in_progress()
        self.events.emit("run_started", wallets=len(self.order), new=added,
                         recovered_in_progress=recovered, counts=self.store.counts())
        processed = 0
        stop_reason = "complete"
        while True:
            if self.stop_event.is_set():
                stop_reason = "stopped"
                break
            gate = self.store.next_run_at()
            if self.clock() < gate:
                self._wait_until(gate, "spacing between attempts")
                continue
            pending = self.store.pending(set(self.wallets))
            if not pending:
                break
            row = pending[0]  # ordered by next_attempt_at, then file position
            if row.next_attempt_at > self.clock():
                self._wait_until(row.next_attempt_at, f"retry backoff for {row.label}")
                continue

            self._attempt(row)
            processed += 1
            # Space the *next* attempt 4-7 min out, unless a throttle already pushed it further.
            self.store.set_next_run_at(max(self.store.next_run_at(),
                                           self.clock() + self._spacing()))
            if self.consecutive_failures >= self.cfg.max_consecutive_failures:
                stop_reason = "circuit_breaker"
                self.events.emit("circuit_breaker_tripped",
                                 consecutive_failures=self.consecutive_failures,
                                 hint="Investigate the server, then rerun; state is preserved.")
                break
            if self.cfg.once:
                stop_reason = "once"
                break
        summary = {"processed": processed, "stop_reason": stop_reason,
                   "counts": self.store.counts()}
        self.events.emit("run_finished", **summary)
        return summary

    def _attempt(self, row) -> None:
        wallet = self.wallets[row.address]
        attempt_no = self.store.mark_in_progress(row.address)
        started = self.clock()
        self.events.emit("attempt_started", label=wallet.label, address=wallet.address,
                         attempt=attempt_no)
        try:
            result = self.client.register(wallet)
        except ClientError as e:
            self._handle_error(row, wallet, attempt_no, started, e)
            return
        except Exception as e:  # unexpected bug: record it, treat as retryable
            self._handle_error(row, wallet, attempt_no, started,
                               ClientError("retryable", f"{type(e).__name__}: {e}"))
            return
        response = redact(result["response"])
        self.store.record_attempt(row.address, attempt_no, started, result["outcome"],
                                  result["status"], None, response)
        self.store.mark_done(row.address, result["outcome"], response)
        self.consecutive_failures = 0
        self.events.emit("attempt_succeeded", label=wallet.label, address=wallet.address,
                         attempt=attempt_no, outcome=result["outcome"],
                         http_status=result["status"], response=response)

    def _handle_error(self, row, wallet, attempt_no, started, e: ClientError) -> None:
        body = redact(e.body)
        self.store.record_attempt(row.address, attempt_no, started, f"error:{e.kind}",
                                  e.status, e.message, body)
        self.consecutive_failures += 1
        base = dict(label=wallet.label, address=wallet.address, attempt=attempt_no,
                    kind=e.kind, http_status=e.status, error=e.message, response=body)

        if e.kind == THROTTLED:
            pause = self._scaled(max(self.cfg.throttle_pause, e.retry_after or 0.0))
            gate = self.clock() + pause
            self.store.set_next_run_at(max(self.store.next_run_at(), gate))
            self.events.emit("throttled_global_pause", until=iso(gate),
                             retry_after=e.retry_after)

        if e.kind == PERMANENT or attempt_no >= self.cfg.max_attempts:
            self.store.mark_failed(row.address, str(e), body)
            self.events.emit("attempt_failed_final", **base)
            return
        next_at = self.clock() + self._backoff(attempt_no, e.retry_after)
        self.store.mark_retry(row.address, next_at, str(e), body)
        self.events.emit("attempt_failed_will_retry", next_attempt_at=iso(next_at), **base)

    # Dry run ----------------------------------------------------------------
    def plan(self) -> list[dict]:
        """Simulate the schedule without network calls or state writes."""
        statuses = {r["address"]: r["status"] for r in self.store.rows()} if self.store else {}
        gate = self.store.next_run_at() if self.store else 0.0
        t = max(self.clock(), gate)
        plan = []
        for addr in self.order:
            status = statuses.get(addr, "pending")
            if status in ("done", "failed"):
                plan.append({"label": self.wallets[addr].label, "address": addr,
                             "action": f"skip ({status})"})
                continue
            plan.append({"label": self.wallets[addr].label, "address": addr,
                         "action": "would register", "at": iso(t)})
            t += self._spacing()
        return plan
