"""Command line entry point: python -m signupmock <command> ..."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import wallets as wallet_mod
from .client import MockSignupClient, NonLocalTargetError
from .mock_server import MockConfig, MockServer
from .runner import EventLog, RunConfig, Runner, iso
from .state import StateStore

PASSPHRASE_ENV = "SIGNUPMOCK_PASSPHRASE"


def _passphrase(args, confirm: bool = False) -> str:
    if getattr(args, "passphrase_file", None):
        return Path(args.passphrase_file).read_text().rstrip("\n")
    if os.environ.get(PASSPHRASE_ENV):
        return os.environ[PASSPHRASE_ENV]
    if not sys.stdin.isatty():
        sys.exit(f"No passphrase: set {PASSPHRASE_ENV} or pass --passphrase-file")
    pw = getpass.getpass("Wallet file passphrase: ")
    if confirm and pw != getpass.getpass("Confirm passphrase: "):
        sys.exit("Passphrases do not match")
    return pw


def _load_wallets(args):
    path = Path(args.wallets)
    if wallet_mod.is_encrypted(path):
        return wallet_mod.load(path, _passphrase(args))
    if wallet_mod.plain_file_is_exposed(path):
        logging.warning("%s is unencrypted and readable by other users; "
                        "run `chmod 600` or encrypt it with the `encrypt` command", path)
    return wallet_mod.load(path)


def cmd_gen_wallets(args) -> int:
    ws = wallet_mod.generate(args.count, args.label_prefix)
    data = wallet_mod.to_plain_json(ws)
    if args.encrypt:
        data = wallet_mod.encrypt(data, _passphrase(args, confirm=True))
    wallet_mod.write_private(args.out, data)
    print(f"Wrote {len(ws)} mock wallets to {args.out}"
          f" ({'encrypted' if args.encrypt else 'plaintext, mode 0600'})")
    return 0


def cmd_encrypt(args) -> int:
    blob = Path(args.input).read_bytes()
    wallet_mod.parse_plain(blob)  # validate before encrypting
    wallet_mod.write_private(args.out, wallet_mod.encrypt(blob, _passphrase(args, confirm=True)))
    print(f"Encrypted {args.input} -> {args.out}. Delete the plaintext file when done.")
    return 0


def cmd_serve(args) -> int:
    cfg = MockConfig(fail_rate=args.fail_rate, throttle_rate=args.throttle_rate,
                     retry_after=args.retry_after, rate_limit_per_min=args.rate_limit_per_min,
                     latency=args.latency, seed=args.seed)
    server = MockServer(args.host, args.port, cfg)
    print(f"Mock signup API listening on {server.base_url}", flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


def cmd_run(args) -> int:
    try:
        client = MockSignupClient(args.base_url, timeout=args.timeout)
    except NonLocalTargetError as e:
        sys.exit(str(e))
    ws = _load_wallets(args)
    cfg = RunConfig(min_delay=args.min_delay, max_delay=args.max_delay,
                    max_attempts=args.max_attempts, backoff_base=args.backoff_base,
                    backoff_max=args.backoff_max, throttle_pause=args.throttle_pause,
                    max_consecutive_failures=args.max_consecutive_failures,
                    time_scale=args.time_scale, once=args.once)
    try:
        cfg.validate()
    except ValueError as e:
        sys.exit(f"Invalid configuration: {e}")
    if args.time_scale < 1:
        logging.warning("time-scale %.4f: waits are compressed (testing only)", args.time_scale)

    if args.dry_run:
        store = StateStore(args.state) if Path(args.state).exists() else None
        runner = Runner(cfg, store, ws, client=None)
        print(f"DRY RUN - no requests will be sent, state is not modified.\n"
              f"Spacing: {cfg.min_delay:.0f}-{cfg.max_delay:.0f}s (x{cfg.time_scale})")
        for step in runner.plan():
            print(f"  {step.get('at', '-'):20}  {step['label']:14} {step['address'][:18]}…  "
                  f"{step['action']}")
        return 0

    store = StateStore(args.state)
    runner = Runner(cfg, store, ws, client, events=EventLog(args.log))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (logging.info("Stop requested; finishing current step"),
                                       runner.stop()))
    summary = runner.run()
    store.close()
    print(json.dumps(summary, indent=2))
    return 2 if summary["stop_reason"] == "circuit_breaker" else 0


def cmd_status(args) -> int:
    if not Path(args.state).exists():
        sys.exit(f"No state file at {args.state}")
    store = StateStore(args.state)
    print(json.dumps(store.counts()))
    gate = store.next_run_at()
    if gate > time.time():
        print(f"next attempt not before {iso(gate)}")
    for r in store.rows():
        line = f"{r['label']:14} {r['address'][:18]}…  {r['status']:11} attempts={r['attempts']}"
        if r["outcome"]:
            line += f" outcome={r['outcome']}"
        if r["last_error"] and r["status"] != "done":
            line += f" last_error={r['last_error']!r}"
        print(line)
    return 0


def cmd_retry_failed(args) -> int:
    n = StateStore(args.state).reset_failed()
    print(f"Reset {n} failed wallet(s) to pending")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="signupmock", description=(
        "Local mock of a wallet signup flow for testing scheduler/persistence "
        "infrastructure. Only talks to a loopback mock server."))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gen-wallets", help="create throwaway Ed25519 test wallets")
    g.add_argument("--count", type=int, default=5)
    g.add_argument("--out", required=True)
    g.add_argument("--label-prefix", default="wallet")
    g.add_argument("--encrypt", action="store_true")
    g.add_argument("--passphrase-file")
    g.set_defaults(func=cmd_gen_wallets)

    e = sub.add_parser("encrypt", help="encrypt a plaintext wallet file")
    e.add_argument("--in", dest="input", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--passphrase-file")
    e.set_defaults(func=cmd_encrypt)

    s = sub.add_parser("serve", help="run the mock signup API")
    s.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1", "localhost"])
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--fail-rate", type=float, default=0.0)
    s.add_argument("--throttle-rate", type=float, default=0.0)
    s.add_argument("--retry-after", type=int, default=60)
    s.add_argument("--rate-limit-per-min", type=int, default=0)
    s.add_argument("--latency", type=float, default=0.0)
    s.add_argument("--seed", type=int)
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("run", help="process the wallet queue against the mock API")
    r.add_argument("--wallets", required=True)
    r.add_argument("--state", default="state.db")
    r.add_argument("--log", default="events.jsonl")
    r.add_argument("--base-url", default="http://127.0.0.1:8787")
    r.add_argument("--passphrase-file")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--once", action="store_true", help="process one wallet then exit")
    r.add_argument("--min-delay", type=float, default=240.0, help="seconds (default 240)")
    r.add_argument("--max-delay", type=float, default=420.0, help="seconds (default 420)")
    r.add_argument("--max-attempts", type=int, default=4)
    r.add_argument("--backoff-base", type=float, default=600.0)
    r.add_argument("--backoff-max", type=float, default=3600.0)
    r.add_argument("--throttle-pause", type=float, default=1800.0)
    r.add_argument("--max-consecutive-failures", type=int, default=3)
    r.add_argument("--time-scale", type=float, default=1.0,
                   help="multiply every wait by this (0<x<=1), for fast local tests")
    r.add_argument("--timeout", type=float, default=15.0)
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="show per-wallet state")
    st.add_argument("--state", default="state.db")
    st.set_defaults(func=cmd_status)

    rf = sub.add_parser("retry-failed", help="move failed wallets back to pending")
    rf.add_argument("--state", default="state.db")
    rf.set_defaults(func=cmd_retry_failed)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        return args.func(args)
    except wallet_mod.WalletFileError as e:
        sys.exit(f"Wallet file error: {e}")
