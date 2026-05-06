#!/usr/bin/env python3
"""
BELIEVE Token Pump Forensics — Full Investigation Script
Uses Helius Enhanced API for parsed transaction data.
"""

import json
import time
import datetime
import requests
import os
from pathlib import Path
from collections import defaultdict

WORKDIR = Path("/home/user/test-launch/believe-forensics")
WORKDIR.mkdir(exist_ok=True)

HELIUS_KEY = "458aa311-b56e-4a35-b3ec-0ba21c33886b"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
HELIUS_API = f"https://api.helius.xyz/v0"

POOL_MAIN  = "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF"
POOL2      = "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7"
MINT       = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"

# Known project wallets (truncated in brief → will resolve)
KNOWN_WALLETS = {
    "5BntgrJfDG": "Believe_Foundation_relay",
    "j1opmdubY8": "Foundation_inflow",
    "AwTxkKrszDzH8WPxqUHS1sTJna5Fagw5xio8kTXWjnfn": "Tax_aggregator_2pct",
    "DaewuPCYwz": "Foundation_outflow_A",
    "CkLygqZbFE": "Foundation_outflow_B",
}

# Pump time boundaries (UTC)
# Flat consolidation ~$850K before 20:14 May 3
# Ignition first candle: 20:14 UTC May 3, 2026
# Peak: May 4, 13:00 UTC (~$0.0078)
# Consolidation: ~$5.5-6M (current)

PUMP_PRE_START  = int(datetime.datetime(2026,4,26,0,0,0).timestamp())   # 7 days before
PUMP_IGNITION   = int(datetime.datetime(2026,5,3,20,14,0).timestamp())  # ignition
PUMP_WINDOW_END = int(datetime.datetime(2026,5,4,18,0,0).timestamp())   # after peak

# Known slots (from binary search):
# 417381868 = 20:00 UTC May 3  (pump start)
# 417384006 = 20:14 UTC May 3  (ignition first notable buys)
# ~417392000 = 21:13 UTC May 3  (post-ignition)

def rpc(method, params, retries=6):
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    for attempt in range(retries):
        try:
            r = requests.post(HELIUS_RPC, json=payload, timeout=30)
            if r.status_code == 429:
                wait = min(4 * 2**attempt, 60)
                print(f"  RPC 429, wait {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                return None
            return d.get("result")
        except Exception as e:
            time.sleep(min(3 * 2**attempt, 30))
    return None

def helius_get_txs(address, before=None, limit=100, tx_type=None):
    """Fetch parsed transactions from Helius enhanced API."""
    url = f"{HELIUS_API}/addresses/{address}/transactions"
    params = {"api-key": HELIUS_KEY, "limit": limit}
    if before:
        params["before"] = before
    if tx_type:
        params["type"] = tx_type
    for attempt in range(6):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = min(4 * 2**attempt, 60)
                print(f"  Helius 429, wait {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(0.3)
            return r.json() if isinstance(r.json(), list) else []
        except Exception as e:
            time.sleep(min(3 * 2**attempt, 30))
    return []

def helius_parse_txs(signatures):
    """Parse multiple transactions at once."""
    url = f"{HELIUS_API}/transactions/"
    params = {"api-key": HELIUS_KEY}
    payload = {"transactions": signatures}
    for attempt in range(5):
        try:
            r = requests.post(url, params=params, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(min(4 * 2**attempt, 60))
                continue
            r.raise_for_status()
            return r.json() if isinstance(r.json(), list) else []
        except Exception as e:
            time.sleep(min(3 * 2**attempt, 30))
    return []

def get_block_time(slot):
    return rpc("getBlockTime", [slot])

def get_sigs_before(address, before_sig=None, limit=1000):
    params = [address, {"limit": limit, "commitment": "finalized"}]
    if before_sig:
        params[1]["before"] = before_sig
    result = rpc("getSignaturesForAddress", params)
    time.sleep(0.3)
    return result or []

def get_block_minimal(slot):
    return rpc("getBlock", [slot, {
        "encoding": "base64", "transactionDetails": "signatures",
        "rewards": False, "commitment": "finalized",
        "maxSupportedTransactionVersion": 0
    }])

def load_cache(fname):
    p = WORKDIR / fname
    return json.loads(p.read_text()) if p.exists() else None

def save_cache(data, fname):
    (WORKDIR / fname).write_text(json.dumps(data, indent=2))

def ts_to_dt(ts):
    if not ts:
        return "?"
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: Find ignition window transactions
# Strategy: Use Helius enhanced API on pool address.
# Find reference sig near pump end (21:30 UTC May 3) then page backwards.
# ─────────────────────────────────────────────────────────────────────────────

print("="*70)
print("SECTION 1: FINDING IGNITION TRANSACTIONS")
print("="*70)

cache_pump = load_cache("pump_txs_enhanced.json")
if cache_pump:
    pump_txs = cache_pump
    print(f"Loaded {len(pump_txs)} cached pump txs")
else:
    pump_txs = []

    # Step 1a: Find a reference sig near slot 417395000 (21:33 UTC May 3)
    ref_sig = None
    print("\nFinding reference sig near pump end (~21:30 UTC May 3)...")
    for slot_try in [417395000, 417393000, 417392000, 417390000, 417388000]:
        bt = get_block_time(slot_try)
        if bt:
            print(f"  Slot {slot_try}: {ts_to_dt(bt)} UTC")
            block = get_block_minimal(slot_try)
            if block and block.get("signatures"):
                ref_sig = block["signatures"][0]
                print(f"  Reference sig: {ref_sig[:60]}...")
                break
        time.sleep(0.3)

    # Step 1b: Use Helius enhanced API to get pool txs from ref_sig backwards
    # The first page (before=ref_sig) will contain transactions from 21:33 backwards
    # We want SWAP transactions in the pool during the pump window
    print(f"\nFetching SWAP txs from pool (Helius enhanced API)...")
    print(f"  Window: {ts_to_dt(PUMP_IGNITION-1800)} to {ts_to_dt(PUMP_IGNITION+5400)} UTC")

    before_cursor = ref_sig
    all_swaps = []
    past_window = False
    page = 0

    while not past_window:
        page += 1
        batch = helius_get_txs(POOL_MAIN, before=before_cursor, limit=100, tx_type="SWAP")
        if not batch:
            print(f"  Page {page}: empty, stopping")
            break

        in_window = []
        for tx in batch:
            ts = tx.get("timestamp", 0)
            if ts >= (PUMP_IGNITION - 1800) and ts <= (PUMP_IGNITION + 5400):
                in_window.append(tx)
            elif ts < (PUMP_IGNITION - 1800):
                past_window = True

        all_swaps.extend(in_window)
        oldest = batch[-1].get("timestamp", 0)
        newest = batch[0].get("timestamp", 0)
        print(f"  Page {page}: {len(batch)} swaps "
              f"({ts_to_dt(oldest)[-8:]} to {ts_to_dt(newest)[-8:]} UTC), "
              f"in_window={len(in_window)}, total={len(all_swaps)}")

        before_cursor = batch[-1]["signature"]

        if len(batch) < 100:
            break

    pump_txs = all_swaps
    save_cache(pump_txs, "pump_txs_enhanced.json")
    print(f"\nSaved {len(pump_txs)} pump window transactions")

# Sort by timestamp ascending
pump_txs.sort(key=lambda x: x.get("timestamp", 0))

print(f"\nPump window: {len(pump_txs)} SWAP txs total")
print(f"Range: {ts_to_dt(pump_txs[0]['timestamp'] if pump_txs else 0)} to "
      f"{ts_to_dt(pump_txs[-1]['timestamp'] if pump_txs else 0)} UTC")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: Parse ignition trades — identify the first buyers
# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*70)
print("SECTION 2: IGNITION TRADE ANALYSIS")
print("="*70)

def parse_helius_swap(tx):
    """Extract trade info from a Helius parsed SWAP transaction."""
    result = {
        "signature": tx.get("signature"),
        "timestamp": tx.get("timestamp"),
        "dt": ts_to_dt(tx.get("timestamp")),
        "fee_payer": tx.get("feePayer"),
        "type": tx.get("type"),
        "description": tx.get("description", ""),
        "side": "unknown",
        "sol_amount": None,
        "token_amount": None,
        "usd_amount": None,
    }

    # Parse native (SOL) transfers
    native = tx.get("nativeTransfers", []) or []
    sol_to_pool = 0
    sol_from_pool = 0
    for nt in native:
        amt = (nt.get("amount") or 0) / 1e9
        frm = nt.get("fromUserAccount", "")
        to  = nt.get("toUserAccount", "")
        if POOL_MAIN in (frm, to) or POOL2 in (frm, to):
            if frm == result["fee_payer"]:
                sol_to_pool += amt
            elif to == result["fee_payer"]:
                sol_from_pool += amt

    # Parse token transfers
    token = tx.get("tokenTransfers", []) or []
    believe_received = 0
    believe_sent = 0
    for tt in token:
        if tt.get("mint") == MINT:
            amt = float(tt.get("tokenAmount") or 0)
            frm = tt.get("fromUserAccount", "")
            to  = tt.get("toUserAccount", "")
            if to == result["fee_payer"]:
                believe_received += amt
            elif frm == result["fee_payer"]:
                believe_sent += amt

    # Also check description for clues
    desc = result["description"].lower()

    if believe_received > 0 or sol_to_pool > 0:
        result["side"] = "buy"
        result["sol_amount"] = sol_to_pool or None
        result["token_amount"] = believe_received or None
    elif believe_sent > 0 or sol_from_pool > 0:
        result["side"] = "sell"
        result["sol_amount"] = sol_from_pool or None
        result["token_amount"] = believe_sent or None

    # Fallback: use description
    if result["side"] == "unknown":
        if "bought" in desc or "swapped" in desc:
            if "believe" in desc or MINT[:6].lower() in desc:
                result["side"] = "buy"
        elif "sold" in desc:
            result["side"] = "sell"

    return result

# Parse all pump txs
parsed_trades = [parse_helius_swap(tx) for tx in pump_txs]

# Show first 20 (ignition trades)
print(f"\nFIRST 20 TRADES IN PUMP WINDOW (sorted by time):")
print(f"{'Time UTC':10} {'Side':5} {'SOL':>8} {'BELIEVE':>14}  {'Trader':46}  {'Sig[:30]'}")
print("-"*120)
for t in parsed_trades[:20]:
    sol_str = f"{t['sol_amount']:.4f}" if t['sol_amount'] else "-"
    tok_str = f"{t['token_amount']:,.0f}" if t['token_amount'] else "-"
    print(f"  {t['dt'][-8:]}  {t['side']:5}  {sol_str:>8}  {tok_str:>14}  {t['fee_payer']:46}  {(t['signature'] or '')[:30]}...")

# Identify first buyers (ignition wallets)
first_buys = [t for t in parsed_trades if t["side"] == "buy"]
print(f"\nTOTAL BUYS IN WINDOW: {len(first_buys)}")
print(f"\nFIRST 10 IGNITION BUYERS:")
for t in first_buys[:10]:
    print(f"  {t['dt']} UTC  |  {t['fee_payer']}  |  {t['sol_amount'] or '?'} SOL  |  {t['token_amount'] or '?'} BELIEVE")

save_cache(parsed_trades, "parsed_trades.json")
ignition_wallets = [t["fee_payer"] for t in first_buys[:10] if t.get("fee_payer")]
save_cache({"ignition_wallets": ignition_wallets}, "ignition_wallets.json")
print(f"\nIgnition wallets saved: {ignition_wallets}")
