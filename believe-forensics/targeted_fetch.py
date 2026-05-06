#!/usr/bin/env python3
"""
Targeted fetch: use known slot anchors to get pool transactions in the pump window.
Strategy:
  1. getBlock(slot ~417390000) to get a reference sig from ~21:00 UTC May 3
  2. getSignaturesForAddress(pool, before=ref_sig) -> returns sigs older than ref_sig
  3. Filter to [20:00, 21:30] UTC May 3
  4. Fetch transaction details for the 1-10 earliest buys (the ignition trades)
"""

import json
import time
import datetime
import requests
from pathlib import Path

WORKDIR = Path("/home/user/test-launch/believe-forensics")
RPC = "https://api.mainnet-beta.solana.com"

POOL_MAIN = "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF"
POOL2 = "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7"
MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"

# Known slots from binary search:
# 417381868 = 20:00:00 UTC May 3 (pump start)
# 417384006 = 20:14:00 UTC May 3 (first notable activity)
# ~417387000 = ~20:26 UTC May 3
# ~417390000 = ~21:00 UTC May 3 (end of ignition hour)

PUMP_START_TS = int(datetime.datetime(2026,5,3,19,50,0).timestamp())
PUMP_END_TS   = int(datetime.datetime(2026,5,3,21,30,0).timestamp())
IGNITION_TS   = int(datetime.datetime(2026,5,3,20,14,0).timestamp())

def rpc_call(method, params, retries=6):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        try:
            r = requests.post(RPC, json=payload, timeout=60)
            if r.status_code == 429:
                wait = min(4 * (2 ** attempt), 60)
                print(f"  429 RL, sleeping {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                return None
            return d.get("result")
        except Exception as e:
            wait = min(3 * (2 ** attempt), 30)
            print(f"  Error: {str(e)[:50]}, retry {wait}s")
            time.sleep(wait)
    return None

def get_block_time(slot):
    return rpc_call("getBlockTime", [slot])

def get_block_minimal(slot):
    """Get block with minimal data to extract one reference signature."""
    return rpc_call("getBlock", [
        slot,
        {
            "encoding": "base64",
            "transactionDetails": "signatures",
            "rewards": False,
            "commitment": "finalized",
            "maxSupportedTransactionVersion": 0,
        }
    ])

def get_block_full(slot):
    """Get block with full parsed transaction data."""
    return rpc_call("getBlock", [
        slot,
        {
            "encoding": "jsonParsed",
            "transactionDetails": "full",
            "rewards": False,
            "commitment": "finalized",
            "maxSupportedTransactionVersion": 0,
        }
    ])

def get_sigs_before(address, before_sig, limit=1000):
    params = [address, {"limit": limit, "commitment": "finalized", "before": before_sig}]
    result = rpc_call("getSignaturesForAddress", params)
    time.sleep(0.5)
    return result or []

def get_tx(sig):
    result = rpc_call("getTransaction", [
        sig,
        {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}
    ])
    time.sleep(0.3)
    return result

def extract_trade(tx_data, sig):
    """Extract buy/sell info from a pool tx."""
    if not tx_data:
        return None

    meta = tx_data.get("meta", {}) or {}
    if meta.get("err"):
        return None

    tx = tx_data.get("transaction", {})
    msg = tx.get("message", {})
    account_keys = msg.get("accountKeys", [])

    # Collect pubkeys
    pubkeys = []
    for k in account_keys:
        pk = k.get("pubkey") if isinstance(k, dict) else str(k)
        pubkeys.append(pk)

    # Check if this involves our pool
    if POOL_MAIN not in pubkeys and POOL2 not in pubkeys and MINT not in pubkeys:
        return None

    signer = pubkeys[0] if pubkeys else None
    block_time = tx_data.get("blockTime")

    # SOL delta for signer
    pre_bal = meta.get("preBalances", [])
    post_bal = meta.get("postBalances", [])
    sol_delta = None
    if pre_bal and post_bal and len(pre_bal) > 0:
        sol_delta = (post_bal[0] - pre_bal[0]) / 1e9

    # Token deltas (BELIEVE)
    token_delta_by_owner = {}
    pre_tok = {tb["accountIndex"]: tb for tb in (meta.get("preTokenBalances") or []) if tb.get("mint") == MINT}
    post_tok = {tb["accountIndex"]: tb for tb in (meta.get("postTokenBalances") or []) if tb.get("mint") == MINT}

    for idx in set(list(pre_tok.keys()) + list(post_tok.keys())):
        pre_amt = float((pre_tok.get(idx, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_amt = float((post_tok.get(idx, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
        owner = (pre_tok.get(idx) or post_tok.get(idx) or {}).get("owner", "")
        if owner:
            token_delta_by_owner[owner] = token_delta_by_owner.get(owner, 0) + (post_amt - pre_amt)

    # Determine trade direction
    side = "unknown"
    if sol_delta is not None:
        if sol_delta < -0.005:
            side = "buy"
        elif sol_delta > 0.005:
            side = "sell"

    # Token amount for signer
    signer_tok = token_delta_by_owner.get(signer, None)

    dt = datetime.datetime.utcfromtimestamp(block_time).strftime("%Y-%m-%d %H:%M:%S") if block_time else "?"

    return {
        "signature": sig,
        "blockTime": block_time,
        "dt_utc": dt,
        "signer": signer,
        "side": side,
        "sol_delta": round(sol_delta, 6) if sol_delta is not None else None,
        "token_delta": round(signer_tok, 2) if signer_tok is not None else None,
        "all_accounts": pubkeys[:8],
        "pool_involved": POOL_MAIN in pubkeys,
    }


# ─── STEP 1: Get reference sig from slot ~417392000 (~21:13 UTC) ──────────────

print("STEP 1: Finding reference signature near pump end time (21:13 UTC May 3)")
ref_slot = 417392000

# Try a few slots to find one that exists
ref_sig = None
for slot_try in [417392000, 417391000, 417390000, 417389000, 417388000]:
    bt = get_block_time(slot_try)
    if bt:
        dt = datetime.datetime.utcfromtimestamp(bt).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  Slot {slot_try}: {dt} UTC")
        if abs(bt - PUMP_END_TS) < 3600:  # within 1 hour of pump end
            block = get_block_minimal(slot_try)
            if block and block.get("signatures"):
                ref_sig = block["signatures"][0]
                ref_slot_actual = slot_try
                ref_bt = bt
                print(f"  => Reference sig: {ref_sig[:60]}... @ {dt} UTC")
                break
    time.sleep(0.3)

if not ref_sig:
    print("ERROR: Could not find reference signature near pump end. Trying alternative approach...")
    # Try to get any sig from the pool and work backwards
    # Use a sig from page 50-60 (which should be around May 3)
    pass


# ─── STEP 2: Get pool signatures before the reference sig ─────────────────────

print("\nSTEP 2: Getting pool sigs before reference sig (should be in pump window)")
pump_sigs = []

if ref_sig:
    # Get up to 1000 sigs before the ref sig
    batch = get_sigs_before(POOL_MAIN, ref_sig, limit=1000)
    for s in batch:
        bt = s.get("blockTime") or 0
        if bt and PUMP_START_TS <= bt <= PUMP_END_TS:
            if s.get("err") is None:
                pump_sigs.append(s)

    print(f"  Got {len(batch)} sigs, {len(pump_sigs)} in pump window [19:50-21:30 UTC May 3]")

    if not pump_sigs and batch:
        # Show range of what we got
        oldest = batch[-1].get("blockTime", 0)
        newest = batch[0].get("blockTime", 0)
        print(f"  Sigs range: {datetime.datetime.utcfromtimestamp(oldest).strftime('%m-%d %H:%M')} to {datetime.datetime.utcfromtimestamp(newest).strftime('%m-%d %H:%M')} UTC")


# ─── FALLBACK: Use getBlock directly on key slots ──────────────────────────────

if not pump_sigs:
    print("\nFallback: Directly fetching pool transactions from key blocks")
    # The ignition window spans slots ~417381000 to ~417392000
    # Let's fetch blocks at 30-slot intervals and look for pool txs

    key_slots = [
        417381000, 417381500, 417382000, 417382500, 417383000, 417383500,
        417384000, 417384500, 417385000, 417385500, 417386000, 417386500,
        417387000, 417387500, 417388000, 417388500, 417389000, 417389500,
        417390000, 417390500, 417391000, 417391500, 417392000,
    ]

    pool_sigs_set = set()
    for slot in key_slots:
        block = get_block_minimal(slot)
        if not block:
            continue
        bt = block.get("blockTime", 0)
        if not bt:
            continue
        dt = datetime.datetime.utcfromtimestamp(bt).strftime("%H:%M:%S")
        sigs = block.get("signatures", [])
        print(f"  Slot {slot} @ {dt} UTC: {len(sigs)} txs in block")

        if PUMP_START_TS <= bt <= PUMP_END_TS:
            # Get full block to find pool transactions
            full_block = get_block_full(slot)
            if full_block:
                txs = full_block.get("transactions", [])
                found = 0
                for tx_obj in txs:
                    meta = tx_obj.get("meta") or {}
                    if meta.get("err"):
                        continue
                    tx_inner = tx_obj.get("transaction", {})
                    sigs_inner = tx_inner.get("signatures", [])
                    sig = sigs_inner[0] if sigs_inner else None

                    msg = tx_inner.get("message", {})
                    keys = msg.get("accountKeys", [])
                    pubkeys_in_tx = [
                        (k.get("pubkey") if isinstance(k, dict) else str(k))
                        for k in keys
                    ]

                    if POOL_MAIN in pubkeys_in_tx or MINT in pubkeys_in_tx:
                        found += 1
                        if sig and sig not in pool_sigs_set:
                            pool_sigs_set.add(sig)
                            pump_sigs.append({
                                "signature": sig,
                                "blockTime": bt,
                                "slot": slot,
                                "err": None,
                            })

                if found:
                    print(f"    => {found} BELIEVE pool txs in this block")
        time.sleep(0.5)

print(f"\nTotal pump window pool sigs: {len(pump_sigs)}")


# ─── STEP 3: Sort by time and fetch tx details for earliest trades ─────────────

pump_sigs.sort(key=lambda x: (x.get("blockTime") or 0))

print(f"\nSTEP 3: Fetching tx details for first {min(50, len(pump_sigs))} pump sigs")
trades = []
cache_file = WORKDIR / "ignition_trades.json"

if cache_file.exists():
    trades = json.loads(cache_file.read_text())
    print(f"  Loaded {len(trades)} cached trades")

already_fetched = {t["signature"] for t in trades}
to_fetch = [s for s in pump_sigs if s["signature"] not in already_fetched][:50]

for i, s in enumerate(to_fetch):
    sig = s["signature"]
    bt = s.get("blockTime", 0)
    dt = datetime.datetime.utcfromtimestamp(bt).strftime("%H:%M:%S") if bt else "?"
    print(f"  [{i+1}/{len(to_fetch)}] {dt} UTC | {sig[:50]}...")

    tx_data = get_tx(sig)
    trade = extract_trade(tx_data, sig)
    if trade:
        trades.append(trade)
        print(f"    => {trade['side']} | SOL {trade['sol_delta']:+.4f} | BELIEVE {trade['token_delta']:+,.0f} | {trade['signer'][:40]}")
    else:
        print(f"    => No pool trade found in this tx")

cache_file.write_text(json.dumps(trades, indent=2))
print(f"\nSaved {len(trades)} trades")


# ─── STEP 4: Display ignition analysis ────────────────────────────────────────

trades.sort(key=lambda x: x.get("blockTime") or 0)
buys = [t for t in trades if t.get("side") == "buy"]

print(f"\n{'='*70}")
print(f"IGNITION WINDOW BUYS: {len(buys)}")
print(f"{'='*70}")
print(f"{'Time UTC':12} {'SOL':>8} {'BELIEVE':>14}  {'Signer':46}")
print("-" * 90)
for t in buys[:30]:
    dt = t.get("dt_utc", "?")[-8:]
    sol = f"{t.get('sol_delta', 0):+.4f}"
    tok = f"{t.get('token_delta', 0):+,.0f}" if t.get("token_delta") is not None else "?"
    signer = t.get("signer", "?") or "?"
    print(f"  {dt}  {sol:>8}  {tok:>14}  {signer}")

# Unique buyers
unique_buyers = {}
for t in buys:
    s = t.get("signer", "")
    if s:
        if s not in unique_buyers:
            unique_buyers[s] = {"count": 0, "total_sol": 0, "first": t.get("blockTime")}
        unique_buyers[s]["count"] += 1
        unique_buyers[s]["total_sol"] += abs(t.get("sol_delta") or 0)

print(f"\nUnique buyers: {len(unique_buyers)}")
for addr, info in sorted(unique_buyers.items(), key=lambda x: x[1]["first"] or 0)[:20]:
    dt = datetime.datetime.utcfromtimestamp(info["first"]).strftime("%H:%M:%S") if info["first"] else "?"
    print(f"  {dt} UTC | {addr} | {info['count']} buys | {info['total_sol']:.4f} SOL")

(WORKDIR / "ignition_buyers.json").write_text(json.dumps([
    {"address": k, **v} for k, v in
    sorted(unique_buyers.items(), key=lambda x: x[1]["first"] or 0)
], indent=2))

print("\nDone. Results saved.")
