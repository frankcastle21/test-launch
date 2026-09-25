"""python -m suibot <command> ...  (see README.md)"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
from pathlib import Path

from . import db as D
from . import keys
from .adapters import ADAPTERS

PASS_ENV = "SUIBOT_PASSPHRASE"


def get_passphrase(args, confirm=False) -> str:
    if args.passphrase_file:
        return Path(args.passphrase_file).read_text().rstrip("\n")
    if os.environ.get(PASS_ENV):
        return os.environ[PASS_ENV]
    if not sys.stdin.isatty():
        sys.exit(f"No passphrase: set {PASS_ENV} or use --passphrase-file")
    pw = getpass.getpass("Keystore passphrase: ")
    if confirm and pw != getpass.getpass("Confirm: "):
        sys.exit("Passphrases do not match")
    return pw


def open_store(args, unlock=True, confirm=False) -> D.Store:
    store = D.Store(args.db)
    if unlock:
        first_time = store.get_meta("kdf_check") is None
        store.unlock(get_passphrase(args, confirm=confirm and first_time))
    return store


def cmd_generate(args):
    store = open_store(args, confirm=True)
    start = store.wallet_count()
    for i in range(args.count):
        addr = store.add_wallet(keys.generate_seed(), f"{args.label_prefix}-{start + i + 1:03d}")
        print(addr)
    print(f"Added {args.count} wallet(s). Back up {args.db} and your passphrase.",
          file=sys.stderr)


def cmd_import(args):
    store = open_store(args, confirm=True)
    src = sys.stdin if args.file == "-" else open(args.file)
    added = skipped = 0
    for n, line in enumerate(src, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key_text, _, label = line.partition(",")
        try:
            seed = keys.parse_private_key(key_text)
        except keys.KeyError_ as e:
            sys.exit(f"line {n}: {e}")
        addr = store.add_wallet(seed, label.strip() or f"imported-{store.wallet_count() + 1:03d}")
        if addr:
            added += 1
            print(addr)
        else:
            skipped += 1
    print(f"Imported {added}, skipped {skipped} already present.", file=sys.stderr)
    if args.file != "-":
        print(f"The keys are now encrypted in {args.db}; consider deleting {args.file}.",
              file=sys.stderr)


def cmd_export(args):
    store = open_store(args)
    w = store.load_wallet(args.address.lower())
    print(keys.encode_suiprivkey(w.seed))


def cmd_list(args):
    store = open_store(args, unlock=False)
    for r in store.all_wallets():
        print(f"{r['label']:16} {r['address']}  {r['status']}")


def cmd_status(args):
    store = open_store(args, unlock=False)
    print(store.counts())
    from .runner import ts
    if store.next_run_at():
        print(f"next signup not before {ts(store.next_run_at())}")
    for r in store.all_wallets():
        when = ts(r["completed_at"] or r["last_attempt_at"]) if (
            r["completed_at"] or r["last_attempt_at"]) else "-"
        line = (f"{r['label']:16} {r['address'][:14]}…  {r['status']:12} "
                f"attempts={r['attempts']}  {when}  {r['result'] or ''}")
        if r["error"]:
            line += f"  error={r['error'][:120]!r}"
        print(line)
    if args.attempts:
        print("\nattempt history:")
        for a in store.attempts():
            print(f"  {ts(a['started_at'])}  {a['address'][:14]}…  "
                  f"{'ok ' if a['success'] else 'ERR'}  {a['result']}  {a['error'] or ''}")


def cmd_reset(args):
    statuses = [s for s, on in ((D.FAILED, args.failed), (D.NEEDS_HUMAN, args.needs_human)) if on]
    if not statuses:
        sys.exit("Pass --failed and/or --needs-human")
    n = open_store(args, unlock=False).reset(tuple(statuses))
    print(f"Moved {n} wallet(s) back to pending")


def cmd_run(args):
    from .browser import WalletBrowser
    from .runner import RunConfig, Runner

    try:
        adapter = ADAPTERS[args.adapter](args.url, referral=args.referral)
    except ValueError as e:
        sys.exit(str(e))
    cfg = RunConfig(min_delay=args.min_delay, max_delay=args.max_delay,
                    max_attempts=args.max_attempts, once=args.once)
    try:
        cfg.validate()
    except ValueError as e:
        sys.exit(f"Invalid config: {e}")
    store = open_store(args)
    runner = Runner(store, adapter, WalletBrowser(headless=not args.headed,
                                                  screenshots_dir=args.screenshots), cfg)
    if args.dry_run:
        print(f"DRY RUN: no browser is opened and nothing is written. Target: {adapter.base_url}")
        for when, label, address in runner.plan():
            print(f"  {when}  {label:16} {address}  would sign up")
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (logging.info("stop requested"), runner.stop()))
    reason = runner.run()
    print(f"stopped: {reason}  {store.counts()}")
    return 3 if reason == "human_check" else 0


def build_parser():
    p = argparse.ArgumentParser(prog="suibot", description=__doc__)
    p.add_argument("--db", default="data/suibot.db")
    p.add_argument("--passphrase-file")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wallets", help="manage wallets").add_subparsers(dest="wcmd",
                                                                          required=True)
    g = w.add_parser("generate", help="create new Ed25519 wallets")
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--label-prefix", default="wallet")
    g.set_defaults(func=cmd_generate)
    i = w.add_parser("import", help="import keys: one `suiprivkey1...|hex[,label]` per line")
    i.add_argument("file", help="path, or - for stdin")
    i.set_defaults(func=cmd_import)
    e = w.add_parser("export", help="print a wallet's suiprivkey (to load it into a wallet app)")
    e.add_argument("address")
    e.set_defaults(func=cmd_export)
    l_ = w.add_parser("list")
    l_.set_defaults(func=cmd_list)

    r = sub.add_parser("run", help="process the queue")
    r.add_argument("--adapter", default="mock", choices=sorted(ADAPTERS))
    r.add_argument("--url", default="http://127.0.0.1:8080")
    r.add_argument("--referral", default="")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--once", action="store_true", help="one wallet, then exit")
    r.add_argument("--headed", action="store_true", help="show the browser (needs a display)")
    r.add_argument("--min-delay", type=float, default=240, help="seconds, default 240")
    r.add_argument("--max-delay", type=float, default=420, help="seconds, default 420")
    r.add_argument("--max-attempts", type=int, default=3)
    r.add_argument("--screenshots", default="data/screenshots")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status")
    s.add_argument("--attempts", action="store_true", help="also print attempt history")
    s.set_defaults(func=cmd_status)

    rs = sub.add_parser("reset", help="move failed / needs-human wallets back to pending")
    rs.add_argument("--failed", action="store_true")
    rs.add_argument("--needs-human", action="store_true")
    rs.set_defaults(func=cmd_reset)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        return args.func(args) or 0
    except keys.WrongPassphrase as e:
        sys.exit(str(e))
