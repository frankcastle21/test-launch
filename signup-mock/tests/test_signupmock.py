import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from signupmock import wallets as W
from signupmock.client import (PERMANENT, ClientError, MockSignupClient,
                               NonLocalTargetError)
from signupmock.mock_server import MockConfig, MockServer
from signupmock.runner import EventLog, RunConfig, Runner
from signupmock.state import StateStore


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        return False


class WalletTests(unittest.TestCase):
    def test_encrypt_roundtrip_and_wrong_passphrase(self):
        ws = W.generate(3)
        blob = W.encrypt(W.to_plain_json(ws), "correct horse")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "w.enc")
            W.write_private(p, blob)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(ws[0].secret_key.hex(), p.read_text())
            loaded = W.load(p, "correct horse")
            self.assertEqual([w.address for w in loaded], [w.address for w in ws])
            with self.assertRaises(W.WalletFileError):
                W.load(p, "wrong")
            with self.assertRaises(W.WalletFileError):
                W.load(p)

    def test_address_mismatch_and_duplicates_rejected(self):
        ws = W.generate(2)
        doc = json.loads(W.to_plain_json(ws))
        doc["wallets"][0]["address"] = ws[1].address
        with self.assertRaises(W.WalletFileError):
            W.parse_plain(json.dumps(doc).encode())
        doc = json.loads(W.to_plain_json([ws[0], ws[0]]))
        with self.assertRaises(W.WalletFileError):
            W.parse_plain(json.dumps(doc).encode())

    def test_signature_verification(self):
        a, b = W.generate(2)
        sig = a.sign(b"hello")
        self.assertTrue(W.verify_signature(a.address, b"hello", sig))
        self.assertFalse(W.verify_signature(a.address, b"other", sig))
        self.assertFalse(W.verify_signature(b.address, b"hello", sig))

    def test_repr_hides_key(self):
        w = W.generate(1)[0]
        self.assertNotIn(w.secret_key.hex(), repr(w))


class ClientGuardTests(unittest.TestCase):
    def test_refuses_non_loopback_targets(self):
        for url in ["https://perpsplexity.app", "http://10.0.0.5:8787",
                    "http://127.0.0.1.evil.example", "ftp://127.0.0.1"]:
            with self.assertRaises(NonLocalTargetError, msg=url):
                MockSignupClient(url)
        MockSignupClient("http://127.0.0.1:1")
        MockSignupClient("http://localhost:1")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = MockServer(config=MockConfig(seed=1)).start()
        self.clock = FakeClock()
        self.wallets = W.generate(4)
        self.db_path = Path(self.tmp.name, "state.db")

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def runner(self, **cfg):
        store = StateStore(self.db_path, clock=self.clock)
        client = MockSignupClient(self.server.base_url, timeout=5)
        events = EventLog(Path(self.tmp.name, "events.jsonl"), clock=self.clock)
        return Runner(RunConfig(**cfg), store, self.wallets, client, events=events,
                      clock=self.clock, sleep=self.clock.sleep, rng=random.Random(7)), store

    def test_processes_all_sequentially_with_4_to_7_minute_spacing(self):
        runner, store = self.runner()
        starts = []
        orig = runner.client.register
        runner.client.register = lambda w: (starts.append(self.clock()), orig(w))[1]
        summary = runner.run()
        self.assertEqual(summary["counts"]["done"], 4)
        self.assertEqual(self.server.state.signup_posts(), 4)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(gaps), 3)
        for g in gaps:
            self.assertGreaterEqual(g, 240)
            self.assertLessEqual(g, 420)
        self.assertEqual(len(set(round(g) for g in gaps)), 3, "delays should be randomized")
        lines = Path(self.tmp.name, "events.jsonl").read_text().splitlines()
        self.assertTrue(any('"attempt_succeeded"' in l for l in lines))
        self.assertFalse(any('"token"' in l and "[redacted]" not in l for l in lines))

    def test_restart_does_not_repeat_completed_wallets_and_honors_wait(self):
        runner, store = self.runner(once=True)
        runner.run()
        store.close()
        self.assertEqual(self.server.state.signup_posts(), 1)
        gate_after_first = StateStore(self.db_path).next_run_at()

        # "Reboot": fresh runner, same state file.
        runner2, store2 = self.runner()
        first_start = []
        orig = runner2.client.register
        runner2.client.register = lambda w: (first_start.append(self.clock()), orig(w))[1]
        runner2.run()
        self.assertEqual(self.server.state.signup_posts(), 4)  # 1 + 3, none repeated
        self.assertGreaterEqual(first_start[0], gate_after_first)
        self.assertEqual(store2.counts()["done"], 4)

    def test_interrupted_attempt_is_recovered_without_duplicate_post(self):
        runner, store = self.runner()
        w = self.wallets[0]
        store.sync_wallets(self.wallets)
        store.mark_in_progress(w.address)       # crashed mid-attempt...
        self.server.state.preregister(w.address)  # ...after the server accepted it
        runner.run()
        row = store.get(w.address)
        self.assertEqual((row.status, row.outcome), ("done", "already_registered"))
        self.assertEqual(self.server.state.signup_posts(), 3)

    def test_other_wallets_proceed_while_one_backs_off(self):
        runner, store = self.runner()
        self.server.state.force_next(503)
        runner.run()
        order = [r["address"] for r in store.db.execute(
            "SELECT address FROM attempts ORDER BY id")]
        self.assertEqual(order[0], self.wallets[0].address)
        self.assertEqual(order[1], self.wallets[1].address)
        self.assertEqual(store.counts()["done"], 4)

    def test_retry_with_backoff_then_success(self):
        self.wallets = self.wallets[:1]
        runner, store = self.runner()
        self.server.state.force_next(503, times=2)  # first two requests fail
        runner.run()
        w = self.wallets[0]
        row = store.get(w.address)
        self.assertEqual((row.status, row.attempts), ("done", 3))
        attempts = store.attempts_for(w.address)
        self.assertEqual([a["result"] for a in attempts],
                         ["error:retryable", "error:retryable", "registered"])
        # Second try waited at least the backoff base (10 min * 0.8 jitter).
        self.assertGreaterEqual(attempts[1]["started_at"] - attempts[0]["finished_at"], 480)

    def test_permanent_error_fails_without_retry(self):
        runner, store = self.runner(max_attempts=4)
        self.server.state.force_next(400, {"error": "bad request"})
        runner.run()
        row = store.get(self.wallets[0].address)
        self.assertEqual((row.status, row.attempts), ("failed", 1))
        self.assertEqual(store.counts()["done"], 3)

    def test_throttle_triggers_global_pause(self):
        runner, store = self.runner()
        self.server.state.force_next(429, headers={"Retry-After": "120"})
        starts = []
        orig = runner.client.register
        runner.client.register = lambda w: (starts.append(self.clock()), orig(w))[1]
        runner.run()
        self.assertGreaterEqual(starts[1] - starts[0], 1800)
        self.assertEqual(store.counts()["done"], 4)

    def test_circuit_breaker_stops_run(self):
        runner, store = self.runner(max_consecutive_failures=3)
        self.server.state.force_next(500, times=50)
        summary = runner.run()
        self.assertEqual(summary["stop_reason"], "circuit_breaker")
        self.assertEqual(summary["processed"], 3)
        self.assertEqual(self.server.state.signup_posts(), 0)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        runner = Runner(RunConfig(), None, self.wallets, client=None, clock=self.clock,
                        rng=random.Random(1))
        plan = runner.plan()
        self.assertEqual([p["action"] for p in plan], ["would register"] * 4)
        self.assertEqual(self.server.state.requests, [])
        self.assertFalse(self.db_path.exists())

    def test_client_classifies_errors(self):
        client = MockSignupClient(self.server.base_url)
        self.server.state.force_next(401)
        with self.assertRaises(ClientError) as cm:
            client.register(self.wallets[0])
        self.assertEqual(cm.exception.kind, PERMANENT)


if __name__ == "__main__":
    unittest.main()
