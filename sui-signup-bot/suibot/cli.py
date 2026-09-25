"""python -m suibot <command> ...  (see README.md)"""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import db as D
from . import keys
from .adapters import ADAPTERS

PASS_ENV = "SUIBOT_PASSPHRASE"


def env(name: str, default=None):
    """Settings come from flags, else SUIBOT_* environment variables, else defaults."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


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


def make_adapter(args):
    try:
        return ADAPTERS[args.adapter](args.url, referral=getattr(args, "referral", ""))
    except ValueError as e:
        sys.exit(str(e))


def cmd_preflight(args):
    """Startup checks used by systemd's ExecStartPre. Exits non-zero on any problem."""
    log = logging.getLogger("suibot")
    store = D.Store(args.db)
    check = store.db.execute("PRAGMA quick_check").fetchone()[0]
    if check != "ok":
        sys.exit(f"Database integrity check failed: {check}")
    if store.get_meta("kdf_check") is None:
        sys.exit("Keystore is not initialised; add wallets first (suibot wallets generate/import)")
    store.unlock(get_passphrase(args))
    counts = store.counts()
    log.info("database ok, passphrase ok, %d wallet(s): %s", store.wallet_count(), counts)
    adapter = make_adapter(args)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + args.wait
    while True:
        try:
            opener.open(adapter.base_url, timeout=5).close()
            break
        except urllib.error.HTTPError:
            break  # any HTTP answer means the service is up
        except (urllib.error.URLError, OSError) as e:
            if time.monotonic() >= deadline:
                sys.exit(f"Signup target {adapter.base_url} not reachable after {args.wait}s: {e}")
            time.sleep(2)
    log.info("target %s is reachable", adapter.base_url)
    if args.browser:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            log.info("chromium %s launches", b.version)
            b.close()
    print("preflight ok")
    return 0


def cmd_backup(args):
    """Consistent online copy of the SQLite DB (keys stay encrypted), keeping the newest N."""
    store = D.Store(args.db)
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = out_dir / f"suibot-{stamp}.db"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    target = sqlite3.connect(dest)
    with target:
        store.db.backup(target)
    target.close()
    backups = sorted(out_dir.glob("suibot-*.db"))
    for old in backups[:-args.keep] if args.keep > 0 else []:
        old.unlink()
    print(f"backup written: {dest} (keeping {args.keep})")
    return 0


def cmd_run(args):
    from .browser import WalletBrowser
    from .runner import RunConfig, Runner

    adapter = make_adapter(args)
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
    p.add_argument("--db", default=env("SUIBOT_DB", "data/suibot.db"))
    p.add_argument("--passphrase-file", default=env("SUIBOT_PASSPHRASE_FILE"))
    p.add_argument("--log-file", default=env("SUIBOT_LOG_FILE"),
                   help="also log here (reopened automatically after logrotate)")
    p.add_argument("-v", "--verbose", action="store_true",
                   default=env("SUIBOT_VERBOSE", "0") == "1")
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

    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--adapter", default=env("SUIBOT_ADAPTER", "mock"), choices=sorted(ADAPTERS))
    target.add_argument("--url", default=env("SUIBOT_URL", "http://127.0.0.1:8080"))

    r = sub.add_parser("run", help="process the queue", parents=[target])
    r.add_argument("--referral", default=env("SUIBOT_REFERRAL", ""))
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--once", action="store_true", help="one wallet, then exit")
    r.add_argument("--headed", action="store_true", help="show the browser (needs a display)")
    r.add_argument("--min-delay", type=float, default=env("SUIBOT_MIN_DELAY", 240),
                   help="seconds, default 240")
    r.add_argument("--max-delay", type=float, default=env("SUIBOT_MAX_DELAY", 420),
                   help="seconds, default 420")
    r.add_argument("--max-attempts", type=int, default=env("SUIBOT_MAX_ATTEMPTS", 3))
    r.add_argument("--screenshots", default=env("SUIBOT_SCREENSHOTS", "data/screenshots"))
    r.set_defaults(func=cmd_run)

    pf = sub.add_parser("preflight", help="check DB, passphrase and target before starting",
                        parents=[target])
    pf.add_argument("--wait", type=int, default=60, help="seconds to wait for the target")
    pf.add_argument("--browser", action="store_true", help="also test-launch Chromium")
    pf.set_defaults(func=cmd_preflight)

    b = sub.add_parser("backup", help="write a consistent copy of the database")
    b.add_argument("--dir", default=env("SUIBOT_BACKUP_DIR", "data/backups"))
    b.add_argument("--keep", type=int, default=env("SUIBOT_BACKUP_KEEP", 14))
    b.set_defaults(func=cmd_backup)

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
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.WatchedFileHandler(args.log_file))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        return args.func(args) or 0
    except keys.WrongPassphrase as e:
        sys.exit(str(e))
