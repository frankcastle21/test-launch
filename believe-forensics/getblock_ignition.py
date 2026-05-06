#!/usr/bin/env python3
"""
Fetch specific blocks around BELIEVE ignition using getBlock RPC.
Known slots:
  - 417381868 = May 3, 2026 20:00:00 UTC (pump start)
  - 417384006 = May 3, 2026 20:14:00 UTC (ignition burst)

We'll scan blocks 417381000..417386000 (pump 20:00-20:28 UTC region)
and filter for transactions involving the pool or mint.
"""

import json
import time
import datetime
import requests
from pathlib import Path

WORKDIR = Path("/home/user/test-launch/believe-forensics")
RPC = "https://api.mainnet-beta.solana.com"

POOL_MAIN = "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF"
MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"

# Other known pool addresses from DexScreener (all Meteora BELIEVE pools)
POOLS = {
    "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF",  # main
    "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7",
}

# Target window: slot 417380000 to 417395000
# 417380000 ≈ 19:54 UTC, 417395000 ≈ 21:34 UTC (~100 min window)
SLOT_START = 417380000
SLOT_END   = 417395000

def rpc_call(method, params, retries=5):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        try:
            r = requests.post(RPC, json=payload, timeout=60)
            if r.status_code == 429:
                wait = min(2 ** attempt * 2, 30)
                print(f"  429 rate limit, sleeping {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                # Slot might not exist (skipped)
                return None
            return d.get("result")
        except Exception as e:
            wait = min(2 ** attempt * 2, 20)
            print(f"  Error: {str(e)[:60]}, retry in {wait}s")
            time.sleep(wait)
    return None

def get_block(slot):
    """Get full block with parsed transactions."""
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

def get_block_time(slot):
    return rpc_call("getBlockTime", [slot])

def scan_block_for_pool_txs(block_data, slot):
    """Extract transactions involving the BELIEVE pool or mint."""
    if not block_data:
        return []

    matches = []
    block_time = block_data.get("blockTime")
    txs = block_data.get("transactions", [])

    for tx_obj in txs:
        meta = tx_obj.get("meta", {}) or {}
        if meta.get("err"):
            continue  # skip failed txs

        tx = tx_obj.get("transaction", {})
        msg = tx.get("message", {})
        account_keys = msg.get("accountKeys", [])

        # Collect all account pubkeys in this tx
        pubkeys = set()
        for k in account_keys:
            pk = k.get("pubkey") if isinstance(k, dict) else str(k)
            pubkeys.add(pk)

        # Also check token balance accounts
        for tb in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
            pubkeys.add(tb.get("mint", ""))
            if "owner" in tb:
                pubkeys.add(tb["owner"])

        # Check if this tx involves our pool or mint
        if not (POOL_MAIN in pubkeys or MINT in pubkeys or any(p in pubkeys for p in POOLS)):
            continue

        # Extract signer (fee payer)
        signer = None
        if account_keys:
            first = account_keys[0]
            signer = first.get("pubkey") if isinstance(first, dict) else str(first)

        # SOL delta for signer
        pre_bal = meta.get("preBalances", [])
        post_bal = meta.get("postBalances", [])
        sol_delta = None
        if pre_bal and post_bal:
            sol_delta = (post_bal[0] - pre_bal[0]) / 1e9

        # Token balance changes (BELIEVE)
        token_delta = None
        pre_tok = {tb["accountIndex"]: tb for tb in (meta.get("preTokenBalances") or []) if tb.get("mint") == MINT}
        post_tok = {tb["accountIndex"]: tb for tb in (meta.get("postTokenBalances") or []) if tb.get("mint") == MINT}
        for idx in set(list(pre_tok.keys()) + list(post_tok.keys())):
            pre_amt = float(pre_tok.get(idx, {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
            post_amt = float(post_tok.get(idx, {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
            owner_pre = pre_tok.get(idx, {}).get("owner", "")
            owner_post = post_tok.get(idx, {}).get("owner", "")
            owner = owner_pre or owner_post
            if owner == signer:
                token_delta = post_amt - pre_amt
                break

        sig = tx.get("signatures", [""])[0]

        # Determine side
        side = "unknown"
        if sol_delta is not None:
            if sol_delta < -0.005:
                side = "buy"
            elif sol_delta > 0.005:
                side = "sell"

        dt = datetime.datetime.utcfromtimestamp(block_time).strftime("%H:%M:%S") if block_time else "?"

        matches.append({
            "slot": slot,
            "blockTime": block_time,
            "dt_utc": dt,
            "signature": sig,
            "signer": signer,
            "side": side,
            "sol_delta": round(sol_delta, 6) if sol_delta is not None else None,
            "token_delta": round(token_delta, 2) if token_delta is not None else None,
        })

    return matches

# ─── MAIN ────────────────────────────────────────────────────────────────────

cache_file = WORKDIR / "ignition_txs_raw.json"
if cache_file.exists():
    print("Loading cached ignition txs...")
    all_matches = json.loads(cache_file.read_text())
else:
    all_matches = []

    # Find blocks that actually exist in the slot range
    # Sample every 100 slots first to find non-skipped blocks
    print(f"Scanning blocks {SLOT_START} to {SLOT_END} for BELIEVE pool txs...")
    print(f"  Target: May 3, 2026 ~20:00-21:30 UTC\n")

    # Use getBlocks to find valid slots in range (more efficient)
    print("Getting list of valid blocks in range...")
    valid_slots = rpc_call("getBlocksWithLimit", [SLOT_START, 5000])
    if valid_slots is None:
        # Fallback: try getBlocks with a smaller range
        valid_slots = rpc_call("getBlocks", [SLOT_START, SLOT_END])

    if valid_slots:
        print(f"Found {len(valid_slots)} valid slots in range")
        valid_slots = [s for s in valid_slots if SLOT_START <= s <= SLOT_END]
    else:
        # Fallback: try consecutive slots, skipping empty
        print("Fallback: trying consecutive slot scan...")
        valid_slots = list(range(SLOT_START, min(SLOT_START + 2000, SLOT_END), 1))

    print(f"Processing {len(valid_slots)} slots...")

    batch_size = 10
    for i in range(0, len(valid_slots), batch_size):
        batch = valid_slots[i:i+batch_size]
        for slot in batch:
            block = get_block(slot)
            if block:
                bt = block.get("blockTime", 0)
                txs = scan_block_for_pool_txs(block, slot)
                if txs:
                    dt_str = datetime.datetime.utcfromtimestamp(bt).strftime("%H:%M:%S") if bt else "?"
                    print(f"  Slot {slot} @ {dt_str} UTC: {len(txs)} BELIEVE txs")
                    all_matches.extend(txs)
            time.sleep(0.15)

        if (i // batch_size) % 5 == 0:
            print(f"  Progress: {i}/{len(valid_slots)} slots processed, {len(all_matches)} txs found")

    cache_file.write_text(json.dumps(all_matches, indent=2))
    print(f"\nSaved {len(all_matches)} transactions")

# Sort by blockTime
all_matches.sort(key=lambda x: (x.get("blockTime") or 0, x.get("slot") or 0))

print(f"\n{'='*70}")
print(f"BELIEVE POOL TRANSACTIONS IN IGNITION WINDOW: {len(all_matches)} total")
print(f"{'='*70}")
print(f"\n{'Time UTC':12} {'Slot':10} {'Side':6} {'SOL':>10} {'BELIEVE':>14} {'Signer':46} {'Sig'}")
print("-" * 130)
for tx in all_matches[:100]:
    sig_short = (tx.get("signature") or "")[:20] + "..."
    signer = tx.get("signer", "?") or "?"
    side = tx.get("side", "?")
    sol = f"{tx.get('sol_delta', 0) or 0:+.4f}"
    tok = f"{tx.get('token_delta', 0) or 0:+,.0f}" if tx.get('token_delta') is not None else "?"
    print(f"  {tx.get('dt_utc','?'):10} {tx.get('slot','?'):10} {side:6} {sol:>10} {tok:>14}  {signer:46}  {sig_short}")

# Identify unique buyers in ignition window
buyers = {}
for tx in all_matches:
    if tx.get("side") == "buy":
        signer = tx.get("signer", "")
        if signer not in buyers:
            buyers[signer] = {"count": 0, "total_sol": 0, "first_ts": tx.get("blockTime")}
        buyers[signer]["count"] += 1
        buyers[signer]["total_sol"] += abs(tx.get("sol_delta") or 0)
        if tx.get("blockTime") and tx.get("blockTime") < buyers[signer]["first_ts"]:
            buyers[signer]["first_ts"] = tx.get("blockTime")

print(f"\n{'='*70}")
print(f"UNIQUE BUYERS IN WINDOW: {len(buyers)}")
print(f"{'='*70}")
sorted_buyers = sorted(buyers.items(), key=lambda x: x[1]["first_ts"])
for addr, info in sorted_buyers[:20]:
    dt = datetime.datetime.utcfromtimestamp(info["first_ts"]).strftime("%H:%M:%S") if info["first_ts"] else "?"
    print(f"  {dt} UTC | {addr} | {info['count']} buys | {info['total_sol']:.4f} SOL")

# Save buyer list
buyer_list = [{"address": k, **v} for k, v in sorted_buyers]
(WORKDIR / "ignition_buyers.json").write_text(json.dumps(buyer_list, indent=2))
print(f"\nSaved buyer list to ignition_buyers.json")
