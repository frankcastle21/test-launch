"""Processes the wallet queue one at a time with randomized spacing."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db as D
from .adapters.base import (HUMAN_CHECK, PERMANENT, RATE_LIMITED, SignupAdapter,
                            SignupError)

log = logging.getLogger("suibot")


@dataclass
class RunConfig:
    min_delay: float = 240.0        # 4 minutes between signups
    max_delay: float = 420.0        # 7 minutes
    max_attempts: int = 3           # per wallet
    retry_backoff: float = 900.0    # first retry waits ~15 min, then doubles
    rate_limit_pause: float = 1800.0  # whole queue pauses 30 min after a rate limit
    once: bool = False

    def validate(self) -> None:
        if not 0 <= self.min_delay <= self.max_delay:
            raise ValueError("need 0 <= min_delay <= max_delay")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Runner:
    def __init__(self, store: D.Store, adapter: SignupAdapter, browser, cfg: RunConfig,
                 clock=time.time, sleep=None, rng=None) -> None:
        cfg.validate()
        self.store, self.adapter, self.browser, self.cfg = store, adapter, browser, cfg
        self.clock = clock
        self.stop_event = threading.Event()
        self.sleep = sleep or self.stop_event.wait
        self.rng = rng or random.SystemRandom()

    def stop(self) -> None:
        self.stop_event.set()

    def _wait_until(self, t: float, why: str) -> None:
        remaining = t - self.clock()
        if remaining > 0:
            log.info("waiting %.0fs until %s (%s)", remaining, ts(t), why)
            self.sleep(remaining)

    def plan(self) -> list[tuple[str, str, str]]:
        """Dry run: (time, label, address) for each pending wallet. No browser, no writes."""
        t = max(self.clock(), self.store.next_run_at())
        out = []
        for row in self.store.pending():
            t = max(t, row["next_attempt_at"])
            out.append((ts(t), row["label"], row["address"]))
            t += self.rng.uniform(self.cfg.min_delay, self.cfg.max_delay)
        return out

    def run(self) -> str:
        """Returns why it stopped: complete | stopped | once | human_check."""
        recovered = self.store.recover_in_progress()
        log.info("starting: %s%s", self.store.counts(),
                 f" (recovered {recovered} interrupted)" if recovered else "")
        while not self.stop_event.is_set():
            pending = self.store.pending()
            if not pending:
                log.info("queue complete: %s", self.store.counts())
                return "complete"
            gate = self.store.next_run_at()
            if self.clock() < gate:
                self._wait_until(gate, "spacing between signups")
                continue
            row = pending[0]
            if row["next_attempt_at"] > self.clock():
                self._wait_until(row["next_attempt_at"], f"retry backoff for {row['label']}")
                continue

            outcome = self._attempt(row)
            self.store.set_next_run_at(max(
                self.store.next_run_at(),
                self.clock() + self.rng.uniform(self.cfg.min_delay, self.cfg.max_delay)))
            if outcome == HUMAN_CHECK:
                log.error("human verification required; stopping. Resolve it, then run "
                          "`reset --needs-human` and restart.")
                return "human_check"
            if self.cfg.once:
                return "once"
        return "stopped"

    def _attempt(self, row) -> str:
        address, label = row["address"], row["label"]
        wallet = self.store.load_wallet(address)
        attempt = self.store.start_attempt(address)
        started = self.clock()
        log.info("[%s] %s attempt %d", label, address, attempt)
        try:
            result = self.browser.run_signup(self.adapter, wallet)
        except SignupError as e:
            kind, error = e.kind, str(e)
        except Exception as e:  # browser crash, bug, etc.: treat as retryable
            kind, error = "retryable", f"{type(e).__name__}: {e}"
        else:
            self.store.finish_attempt(address, started, status=D.DONE, success=True,
                                      result=result)
            log.info("[%s] success: %s", label, result)
            return result

        if kind == HUMAN_CHECK:
            self.store.finish_attempt(address, started, status=D.NEEDS_HUMAN, success=False,
                                      result=kind, error=error)
            return kind
        if kind == RATE_LIMITED:
            pause_until = self.clock() + self.cfg.rate_limit_pause
            self.store.set_next_run_at(max(self.store.next_run_at(), pause_until))
            log.warning("rate limited; pausing the whole queue until %s", ts(pause_until))
        if kind == PERMANENT or attempt >= self.cfg.max_attempts:
            self.store.finish_attempt(address, started, status=D.FAILED, success=False,
                                      result=kind, error=error)
            log.error("[%s] failed permanently: %s", label, error)
            return kind
        retry_at = self.clock() + self.cfg.retry_backoff * 2 ** (attempt - 1)
        self.store.finish_attempt(address, started, status=D.PENDING, success=False,
                                  result=kind, error=error, next_attempt_at=retry_at)
        log.warning("[%s] %s: %s (retry after %s)", label, kind, error, ts(retry_at))
        return kind
