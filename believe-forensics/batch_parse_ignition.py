#!/usr/bin/env python3
"""Batch-parse ignition sigs via Helius and paginate APVT back to May 3."""
import json, time, requests
from pathlib import Path
from collections import defaultdict

WORKDIR = Path("/home/user/test-launch/believe-forensics")
KEY = "458aa311-b56e-4a35-b3ec-0ba21c33886b"
RPC_URL   = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
PARSE_URL = f"https://api.helius.xyz/v0/transactions/?api-key={KEY}"
ENH_URL   = f"https://api.helius.xyz/v0/addresses"

MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"
WSOL = "So11111111111111111111111111111111111111112"
POOL_MAIN = "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF"
POOL2     = "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7"

import datetime
def ts(t): return datetime.datetime.utcfromtimestamp(t).strftime("%H:%M:%S") if t else "?"

def parse_batch(sigs, retries=4):
    for att in range(retries):
        try:
            r = requests.post(PARSE_URL, json={"transactions": sigs}, timeout=90)
            if r.status_code == 429:
                w = min(15*2**att, 120); print(f"  Parse 429 wait {w}s"); time.sleep(w); continue
            r.raise_for_status()
            time.sleep(0.5)
            d = r.json()
            return d if isinstance(d, list) else []
        except Exception as e:
            print(f"  parse_batch error: {e}"); time.sleep(min(10*2**att, 60))
    return []

def helius_enhanced(addr, before=None, limit=100):
    url = f"{ENH_URL}/{addr}/transactions"
    params = {"api-key": KEY, "limit": limit}
    if before: params["before"] = before
    for att in range(5):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                w = min(8*2**att,60); print(f"  Enh 429 wait {w}s"); time.sleep(w); continue
            r.raise_for_status()
            time.sleep(0.4)
            d = r.json()
            return d if isinstance(d, list) else []
        except Exception as e:
            time.sleep(min(5*2**att,30))
    return []

def extract_believe_flow(tx):
    """Get BELIEVE net flows for all accounts."""
    fp = tx.get("feePayer","")
    ts_v = tx.get("timestamp",0)
    sig  = tx.get("signature","")
    
    if tx.get("transactionError"): return None
    
    believe_net = defaultdict(float)
    wsol_net    = defaultdict(float)
    sol_net     = defaultdict(float)
    
    for tt in (tx.get("tokenTransfers",[]) or []):
        mint = tt.get("mint","")
        amt  = float(tt.get("tokenAmount") or 0)
        frm  = tt.get("fromUserAccount","") or ""
        to   = tt.get("toUserAccount","")   or ""
        if mint == MINT:
            believe_net[frm] -= amt
            believe_net[to]  += amt
        elif mint == WSOL:
            wsol_net[frm] -= amt
            wsol_net[to]  += amt
    
    for nt in (tx.get("nativeTransfers",[]) or []):
        amt = (nt.get("amount") or 0) / 1e9
        frm = nt.get("fromUserAccount","") or ""
        to  = nt.get("toUserAccount","")   or ""
        sol_net[frm] -= amt
        sol_net[to]  += amt
    
    # Find the net BELIEVE receiver (the buyer's delivery address)
    max_believe = 0
    max_addr = ""
    for addr, net in believe_net.items():
        if net > max_believe:
            max_believe = net
            max_addr = addr
    
    fp_believe = believe_net.get(fp, 0)
    fp_wsol    = wsol_net.get(fp, 0)
    fp_sol     = sol_net.get(fp, 0)
    
    return {
        "sig": sig, "ts": ts_v, "dt": ts(ts_v),
        "feePayer": fp,
        "fp_believe_net": round(fp_believe, 4),
        "fp_wsol_net": round(fp_wsol, 6),
        "fp_sol_net": round(fp_sol, 6),
        "believe_net": dict(believe_net),
        "wsol_net": dict(wsol_net),
        "max_believe_receiver": max_addr,
        "max_believe_amount": round(max_believe, 4),
        "type": tx.get("type",""),
    }

# ── 1. Batch-parse all ignition sigs we have collected ──────────────────────

ignition_sigs_raw = json.load(open(WORKDIR/"ignition_sample_txs.json"))
sigs_to_parse = [s["sig"] for s in ignition_sigs_raw if s.get("sig")]

# Also grab sigs from wallet_traces_summary if available
try:
    wt = json.load(open(WORKDIR/"wallet_traces_summary.json"))
    for entry in wt:
        if entry.get("sig"): sigs_to_parse.append(entry["sig"])
except: pass

sigs_to_parse = list(dict.fromkeys(sigs_to_parse))  # deduplicate
print(f"Batch-parsing {len(sigs_to_parse)} ignition signatures...")

parsed_ignition = parse_batch(sigs_to_parse)
print(f"Got {len(parsed_ignition)} parsed results")

flows = []
for tx in parsed_ignition:
    f = extract_believe_flow(tx)
    if f: flows.append(f)

print(f"\n{'='*70}")
print(f"IGNITION TRANSACTION FLOWS (19:58 UTC May 3, 2026)")
print(f"{'='*70}")
for f in sorted(flows, key=lambda x: x["ts"]):
    print(f"\n  {f['dt']} | {f['type']} | sig={f['sig'][:50]}")
    print(f"  feePayer: {f['feePayer']}")
    print(f"  fp_believe_net: {f['fp_believe_net']:+,.0f} | fp_wsol_net: {f['fp_wsol_net']:+.4f} | fp_sol_net: {f['fp_sol_net']:+.4f}")
    print(f"  Max BELIEVE receiver: {f['max_believe_receiver']} ({f['max_believe_amount']:,.0f} BELIEVE)")
    # Show all believe flows
    for addr, amt in sorted(f['believe_net'].items(), key=lambda x: abs(x[1]), reverse=True)[:5]:
        if abs(amt) > 100:
            print(f"    BELIEVE {addr[:20]}: {amt:+,.0f}")

json.dump(flows, open(WORKDIR/"ignition_flows.json","w"), indent=2)
print(f"\nSaved {len(flows)} ignition flows to ignition_flows.json")

