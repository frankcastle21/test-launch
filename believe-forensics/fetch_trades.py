#!/usr/bin/env python3
"""
BELIEVE Token Forensics — Solana transaction fetcher
Uses public Solana RPC to fetch trade signatures and transaction details
around the pump ignition window.
"""

import json
import time
import datetime
import requests
import sys
from pathlib import Path

WORKDIR = Path("/home/user/test-launch/believe-forensics")
WORKDIR.mkdir(exist_ok=True)

MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"
POOL_MAIN = "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF"

# Ignition window (UTC): May 3, 2026 19:45 to 21:30
IGNITION_START_TS = 1777837500  # 19:45 UTC May 3
IGNITION_END_TS   = 1777844200  # 21:30 UTC May 3

# Pre-pump week: April 26 to May 3 19:45
PREPUMP_START_TS = 1777202400   # April 26, 2026 00:00 UTC
PREPUMP_END_TS   = IGNITION_START_TS

RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
]
rpc_idx = 0

def rpc(method, params, retries=5):
    global rpc_idx
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        url = RPC_URLS[rpc_idx % len(RPC_URLS)]
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                rpc_idx += 1
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                print(f"  RPC error: {d['error']}")
                time.sleep(1)
                continue
            return d.get("result")
        except Exception as e:
            print(f"  Error {e}, retrying...")
            time.sleep(2 ** attempt)
    return None

def get_signatures_for_address(address, before=None, until=None, limit=1000):
    params = [address, {"limit": limit, "commitment": "finalized"}]
    if before:
        params[1]["before"] = before
    if until:
        params[1]["until"] = until
    return rpc("getSignaturesForAddress", params) or []

def get_transaction(sig):
    params = [sig, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}]
    return rpc("getTransaction", params)

def fetch_sigs_in_window(address, start_ts, end_ts, label=""):
    """Fetch all transaction signatures in a time window for an address."""
    print(f"\n[{label}] Fetching signatures for {address[:20]}... ({datetime.datetime.utcfromtimestamp(start_ts).strftime('%Y-%m-%d %H:%M')} to {datetime.datetime.utcfromtimestamp(end_ts).strftime('%Y-%m-%d %H:%M')} UTC)")

    all_sigs = []
    before_cursor = None
    page = 0

    while True:
        batch = get_signatures_for_address(address, before=before_cursor, limit=1000)
        if not batch:
            break

        filtered = []
        done = False
        for s in batch:
            bt = s.get("blockTime", 0)
            if bt is None:
                continue
            if bt > end_ts:
                continue  # too recent, skip
            if bt < start_ts:
                done = True
                break  # too old, stop
            if s.get("err") is None:  # only successful txs
                filtered.append(s)

        all_sigs.extend(filtered)
        page += 1
        print(f"  Page {page}: {len(batch)} sigs fetched, {len(filtered)} in window, total={len(all_sigs)}")

        if done or len(batch) < 1000:
            break

        before_cursor = batch[-1]["signature"]
        time.sleep(0.5)  # rate limit courtesy

    print(f"  => {len(all_sigs)} signatures in window")
    return all_sigs

def save_json(data, fname):
    path = WORKDIR / fname
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved to {path}")

def load_json(fname):
    path = WORKDIR / fname
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None

# ─── STEP 1: Fetch signatures around ignition window ──────────────────────────
print("=" * 70)
print("STEP 1: Fetching ignition window transactions (pool + mint)")
print("=" * 70)

cache = load_json("sigs_ignition_pool.json")
if cache:
    print(f"  Loaded {len(cache)} pool signatures from cache")
    pool_sigs = cache
else:
    pool_sigs = fetch_sigs_in_window(POOL_MAIN, IGNITION_START_TS, IGNITION_END_TS, "POOL_IGNITION")
    save_json(pool_sigs, "sigs_ignition_pool.json")

print(f"\nPool ignition sigs: {len(pool_sigs)}")
for s in pool_sigs[:5]:
    dt = datetime.datetime.utcfromtimestamp(s['blockTime']).strftime('%Y-%m-%d %H:%M:%S')
    print(f"  {dt} UTC | {s['signature'][:60]}...")

# ─── STEP 2: Fetch signatures for pre-pump week (looking for pre-positioning) ──
print("\n" + "=" * 70)
print("STEP 2: Fetching pre-pump week signatures (last 7 days before ignition)")
print("=" * 70)

# For pre-pump analysis, just get mint-level signatures
# The pool level would be too many. Let's use a narrower window first
cache2 = load_json("sigs_prepump_pool.json")
if cache2:
    print(f"  Loaded {len(cache2)} pre-pump pool signatures from cache")
    prepump_sigs = cache2
else:
    # April 28 to May 3 19:45 (the observable flat period)
    pre_start = int(datetime.datetime(2026,4,28,0,0,0).timestamp())
    prepump_sigs = fetch_sigs_in_window(POOL_MAIN, pre_start, IGNITION_START_TS, "POOL_PREPUMP")
    save_json(prepump_sigs, "sigs_prepump_pool.json")

print(f"\nPre-pump pool sigs: {len(prepump_sigs)}")

print("\n[DONE] Signature fetch complete. Now fetching transaction details...")
