import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mock_site.server import MockSite, make_server
from suibot import db as D
from suibot import keys
from suibot.adapters import MockSiteAdapter
from suibot.adapters.base import HUMAN_CHECK
from suibot.browser import WalletBrowser
from suibot.runner import RunConfig, Runner


class KeyTests(unittest.TestCase):
    def test_suiprivkey_roundtrip_and_hex(self):
        seed = keys.generate_seed()
        self.assertEqual(keys.parse_private_key(keys.encode_suiprivkey(seed)), seed)
        self.assertEqual(keys.parse_private_key(seed.hex()), seed)
        with self.assertRaises(keys.KeyError_):
            keys.parse_private_key(keys.encode_suiprivkey(seed)[:-1] + "q")

    def test_rejects_non_ed25519(self):
        k = keys.bech32_encode("suiprivkey", bytes([1]) + os.urandom(32))  # secp256k1 flag
        with self.assertRaises(keys.KeyError_):
            keys.parse_private_key(k)

    def test_personal_message_signature(self):
        seed = keys.generate_seed()
        addr = keys.address_of(keys.pubkey_of(seed))
        sig = keys.sign_personal_message(seed, b"x" * 300)
        self.assertTrue(keys.verify_personal_message(addr, b"x" * 300, sig))
        self.assertFalse(keys.verify_personal_message(addr, b"y" * 300, sig))


class StoreTests(unittest.TestCase):
    def test_encrypted_at_rest_and_wrong_passphrase(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "s.db")
            s = D.Store(path)
            s.unlock("pw")
            seed = keys.generate_seed()
            addr = s.add_wallet(seed, "w1")
            self.assertIsNone(s.add_wallet(seed, "dup"))
            s.close()
            self.assertNotIn(seed.hex().encode(), path.read_bytes())
            s2 = D.Store(path)
            with self.assertRaises(keys.WrongPassphrase):
                s2.unlock("nope")
            s2.unlock("pw")
            self.assertEqual(s2.load_wallet(addr).seed, seed)


class EndToEndTests(unittest.TestCase):
    """Real headless Chromium against the local mock site."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = MockSite()
        self.httpd = make_server(self.site, port=0)
        self.httpd.quiet = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.db_path = Path(self.tmp.name, "bot.db")
        self.store = D.Store(self.db_path)
        self.store.unlock("pw")
        self.addrs = [self.store.add_wallet(keys.generate_seed(), f"w{i}") for i in range(3)]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def runner(self, **kw):
        cfg = RunConfig(min_delay=0.3, max_delay=0.6, retry_backoff=0.2, **kw)
        return Runner(self.store, MockSiteAdapter(self.url), WalletBrowser(), cfg)

    def test_signs_up_all_wallets_sequentially_and_resumes(self):
        r = self.runner(once=True)
        self.assertEqual(r.run(), "once")
        self.assertEqual(len(self.site.signups), 1)

        # Simulated restart: new store object, same DB. Completed wallet is not repeated.
        self.store.close()
        self.store = D.Store(self.db_path)
        self.store.unlock("pw")
        t0 = time.time()
        self.assertEqual(self.runner().run(), "complete")
        self.assertEqual(set(self.site.signups), set(self.addrs))
        self.assertEqual(self.site.posts, 3)
        self.assertGreaterEqual(time.time() - t0, 0.3 * 2)  # spacing respected
        rows = self.store.attempts()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(a["success"] and a["result"] == "registered" for a in rows))

    def test_already_registered_wallet_is_detected(self):
        self.site.signups[self.addrs[0]] = {"address": self.addrs[0]}
        self.runner().run()
        row = [w for w in self.store.all_wallets() if w["address"] == self.addrs[0]][0]
        self.assertEqual((row["status"], row["result"]), ("done", "already_registered"))
        self.assertEqual(self.site.posts, 2)  # page saw the existing signup, never POSTed

    def test_captcha_stops_the_run(self):
        self.site.captcha = True
        self.assertEqual(self.runner().run(), HUMAN_CHECK)
        self.assertEqual(self.store.counts()[D.NEEDS_HUMAN], 1)
        self.assertEqual(self.store.counts()[D.PENDING], 2)
        self.assertEqual(self.site.signups, {})

    def test_rate_limit_retries_later(self):
        self.site.rate_limit_per_min = 1
        r = self.runner(once=True, max_attempts=2)
        r.cfg.rate_limit_pause = 0.5
        r.run()
        r.run()  # second wallet is rate limited
        counts = self.store.counts()
        self.assertEqual(counts[D.DONE], 1)
        second = self.store.attempts(self.addrs[1])
        self.assertEqual(second[0]["result"], "rate_limited")
        self.assertGreater(self.store.next_run_at(), time.time())

    def test_dry_run_does_nothing(self):
        plan = self.runner().plan()
        self.assertEqual([p[2] for p in plan], self.addrs)
        self.assertEqual(self.site.posts, 0)
        self.assertEqual(self.store.attempts(), [])

    def test_adapter_refuses_remote_url(self):
        with self.assertRaises(ValueError):
            MockSiteAdapter("https://example.com")

    def test_browser_blocks_external_requests(self):
        wallet = self.store.load_wallet(self.addrs[0])

        class Probe(MockSiteAdapter):
            def signup(self, page, address):
                page.goto(f"{self.base_url}/signup")
                return page.evaluate("fetch('https://example.com').then(() => 'reached')"
                                     ".catch(() => 'blocked')")

        self.assertEqual(WalletBrowser().run_signup(Probe(self.url), wallet), "blocked")


if __name__ == "__main__":
    unittest.main()
