#!/usr/bin/env python3
"""
Binary search for the pump ignition slot, then fetch ignition transactions.
Target: May 3, 2026 ~20:14 UTC (BELIEVE ignition)
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

# Ignition: May 3, 2026 20:14 UTC
IGNITION_TS = int(datetime.datetime(2026,5,3,20,14,0).timestamp())  # 1777839240
PUMP_START_TS = int(datetime.datetime(2026,5,3,20,0,0).timestamp())  # 1777838400
PUMP_END_TS   = int(datetime.datetime(2026,5,3,21,30,0).timestamp())  # 1777844200
PREPUMP_START = int(datetime.datetime(2026,4,26,0,0,0).timestamp())

def rpc_call(method, params, retries=6):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        try:
            r = requests.post(RPC, json=payload, timeout=30)
            if r.status_code == 429:
                wait = min(2 ** attempt, 32)
                print(f"  429 rate limit, sleeping {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                print(f"  RPC error: {d['error']['message'][:80]}")
                time.sleep(2)
                continue
            return d.get("result")
        except Exception as e:
            wait = min(2 ** attempt, 16)
            print(f"  Exception: {str(e)[:60]}, retrying in {wait}s...")
            time.sleep(wait)
    return None

def get_slot():
    return rpc_call("getSlot", [])

def get_block_time(slot):
    return rpc_call("getBlockTime", [slot])

def binary_search_slot(target_ts, known_slot, known_ts):
    """Find a slot close to target_ts using binary search."""
    # Solana: ~400ms per slot = 2.5 slots/sec
    SLOTS_PER_SEC = 2.5

    # Estimate target slot from known point
    delta_sec = target_ts - known_ts
    est_slot = int(known_slot + delta_sec * SLOTS_PER_SEC)
    print(f"\nBinary search: target_ts={target_ts} ({datetime.datetime.utcfromtimestamp(target_ts).strftime('%Y-%m-%d %H:%M:%S')} UTC)")
    print(f"  Known slot: {known_slot} @ {known_ts}")
    print(f"  Estimated slot: {est_slot}")

    # Iteratively refine
    slot = est_slot
    for i in range(12):
        bt = get_block_time(slot)
        if bt is None:
            # slot might not exist (skipped), try adjacent
            for delta in [1, -1, 5, -5, 10, -10, 50, -50]:
                bt = get_block_time(slot + delta)
                if bt is not None:
                    slot += delta
                    break
        if bt is None:
            print(f"  Iteration {i}: slot {slot} has no blocktime, adjusting by estimate")
            slot = int(slot + (target_ts - (known_ts + (slot - known_slot)/SLOTS_PER_SEC)) * SLOTS_PER_SEC)
            continue

        diff = bt - target_ts
        print(f"  Iteration {i}: slot={slot} bt={bt} ({datetime.datetime.utcfromtimestamp(bt).strftime('%H:%M:%S')} UTC), diff={diff:+.0f}s")

        if abs(diff) < 5:  # within 5 seconds, close enough
            print(f"  => Found slot {slot}")
            return slot

        # Adjust
        slot = int(slot - diff * SLOTS_PER_SEC)
        time.sleep(0.3)

    return slot

def get_signatures_page(address, before=None, limit=1000):
    params = [address, {"limit": limit, "commitment": "finalized"}]
    if before:
        params[1]["before"] = before
    result = rpc_call("getSignaturesForAddress", params)
    time.sleep(0.4)
    return result or []

def fetch_tx(sig, retries=3):
    for i in range(retries):
        result = rpc_call("getTransaction", [
            sig,
            {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}
        ])
        if result:
            return result
        time.sleep(1)
    return None

def extract_swap_info(tx_data):
    """Extract swap/trade info from a Meteora DLMM pool transaction."""
    info = {
        "sig": None,
        "ts": None,
        "dt": None,
        "signer": None,
        "side": None,  # buy or sell
        "sol_amount": None,
        "token_amount": None,
        "program_ids": [],
        "accounts": [],
    }

    if not tx_data:
        return info

    meta = tx_data.get("meta", {})
    tx = tx_data.get("transaction", {})

    info["ts"] = tx_data.get("blockTime")
    if info["ts"]:
        info["dt"] = datetime.datetime.utcfromtimestamp(info["ts"]).strftime("%Y-%m-%d %H:%M:%S")

    # Get signers
    msg = tx.get("message", {})
    account_keys = msg.get("accountKeys", [])
    if account_keys:
        # First account is usually the fee payer / signer
        first = account_keys[0]
        if isinstance(first, dict):
            info["signer"] = first.get("pubkey")
        else:
            info["signer"] = str(first)

    info["accounts"] = [
        (k.get("pubkey") if isinstance(k, dict) else str(k))
        for k in account_keys[:10]
    ]

    # Get program ids from instructions
    instructions = msg.get("instructions", [])
    for ix in instructions:
        pid = ix.get("programId", "")
        if pid and pid not in info["program_ids"]:
            info["program_ids"].append(pid)

    # SOL balance changes
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    if pre_balances and post_balances and len(pre_balances) == len(post_balances):
        signer_delta = post_balances[0] - pre_balances[0]
        info["sol_amount"] = signer_delta / 1e9  # lamports to SOL

    # Token balance changes for the signer
    pre_token = meta.get("preTokenBalances", []) or []
    post_token = meta.get("postTokenBalances", []) or []

    for pt in post_token:
        if pt.get("mint") == MINT and pt.get("accountIndex") == 1:
            pre_amt = next((p["uiTokenAmount"]["uiAmount"] for p in pre_token
                          if p.get("accountIndex") == pt.get("accountIndex")), 0) or 0
            post_amt = pt["uiTokenAmount"].get("uiAmount") or 0
            info["token_amount"] = post_amt - pre_amt
            break

    # Determine buy/sell from SOL delta: if signer spent SOL, it's a buy
    if info["sol_amount"] is not None:
        if info["sol_amount"] < -0.01:
            info["side"] = "buy"  # spent SOL
        elif info["sol_amount"] > 0.01:
            info["side"] = "sell"  # received SOL

    return info

# ─── MAIN ────────────────────────────────────────────────────────────────────

print("=" * 70)
print("STEP 1: Get current slot and binary-search for ignition block")
print("=" * 70)

current_slot = get_slot()
current_ts = get_block_time(current_slot)
print(f"Current slot: {current_slot}, blocktime: {current_ts}")
if current_ts:
    print(f"Current time: {datetime.datetime.utcfromtimestamp(current_ts).strftime('%Y-%m-%d %H:%M:%S')} UTC")

# Find slot near ignition time
ignition_slot = binary_search_slot(IGNITION_TS, current_slot, current_ts)
print(f"\nIgnition slot estimate: {ignition_slot}")

# Find slot near pump start (20:00 UTC)
pump_start_slot = binary_search_slot(PUMP_START_TS, current_slot, current_ts)
print(f"Pump start slot estimate: {pump_start_slot}")

print("\n" + "=" * 70)
print("STEP 2: Page backwards through pool signatures from current to pump")
print("=" * 70)

# Strategy: Start from pool's recent sigs and page back until we hit the pump window
# Cache signatures in the pump window

pump_sigs_file = WORKDIR / "pump_window_sigs.json"
if pump_sigs_file.exists():
    pump_sigs = json.loads(pump_sigs_file.read_text())
    print(f"Loaded {len(pump_sigs)} cached pump window sigs")
else:
    pump_sigs = []
    before_cursor = None
    page = 0
    found_window = False
    past_window = False

    while not past_window:
        page += 1
        batch = get_signatures_page(POOL_MAIN, before=before_cursor)
        if not batch:
            print(f"  Page {page}: empty batch, stopping")
            break

        in_window = []
        oldest_bt = None
        for s in batch:
            bt = s.get("blockTime") or 0
            oldest_bt = bt  # last in batch is oldest (they're in reverse chron order)
            if s.get("err") is not None:
                continue  # skip failed txs
            if PUMP_START_TS <= bt <= PUMP_END_TS:
                in_window.append(s)
                found_window = True
            elif bt < PUMP_START_TS:
                past_window = True

        pump_sigs.extend(in_window)
        newest_bt = batch[0].get("blockTime", 0)
        print(f"  Page {page}: {len(batch)} sigs, "
              f"range {datetime.datetime.utcfromtimestamp(oldest_bt).strftime('%m-%d %H:%M') if oldest_bt else '?'}"
              f" to {datetime.datetime.utcfromtimestamp(newest_bt).strftime('%m-%d %H:%M') if newest_bt else '?'} UTC"
              f" | in_window={len(in_window)} | total_pump={len(pump_sigs)}")

        before_cursor = batch[-1]["signature"]

        if len(batch) < 1000:
            break

    pump_sigs_file.write_text(json.dumps(pump_sigs, indent=2))
    print(f"\nSaved {len(pump_sigs)} pump window signatures")

print(f"\nTotal pump window sigs: {len(pump_sigs)}")

# Sort by blockTime ascending
pump_sigs.sort(key=lambda x: x.get("blockTime", 0))

# Show first 20 (earliest in pump window)
print("\nEarliest transactions in pump window (first 20):")
for s in pump_sigs[:20]:
    bt = s.get("blockTime", 0)
    dt = datetime.datetime.utcfromtimestamp(bt).strftime("%H:%M:%S") if bt else "?"
    print(f"  {dt} UTC | slot={s.get('slot')} | {s['signature'][:60]}...")

# Save slot info
slot_info = {
    "current_slot": current_slot,
    "current_ts": current_ts,
    "ignition_slot_est": ignition_slot,
    "pump_start_slot_est": pump_start_slot,
    "pump_start_ts": PUMP_START_TS,
    "pump_end_ts": PUMP_END_TS,
}
(WORKDIR / "slot_info.json").write_text(json.dumps(slot_info, indent=2))
print("\nSlot info saved.")
