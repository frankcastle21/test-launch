#!/usr/bin/env python3
"""Fetch history for delivery wallets: Bs6cTDM6 and 62N5myBG3E2pB1AhLdDi9SFfLP78Y7jHNkLa4qt5NjLK"""
import json, time, requests, datetime
from pathlib import Path
from collections import defaultdict

WORKDIR = Path("/home/user/test-launch/believe-forensics")
KEY = "458aa311-b56e-4a35-b3ec-0ba21c33886b"
ENH_URL = f"https://api.helius.xyz/v0/addresses"
MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"
WSOL = "So11111111111111111111111111111111111111112"

def ts(t): return datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "?"

def fetch_history(addr, limit=100, before=None):
    url = f"{ENH_URL}/{addr}/transactions"
    params = {"api-key": KEY, "limit": limit}
    if before: params["before"] = before
    for att in range(5):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                w = min(8*2**att,60); print(f"  429 wait {w}s"); time.sleep(w); continue
            r.raise_for_status()
            time.sleep(0.5)
            d = r.json()
            return d if isinstance(d, list) else []
        except Exception as e:
            print(f"  Error: {e}"); time.sleep(min(5*2**att,30))
    return []

def summarize_wallet(addr, txs, label=""):
    print(f"\n{'='*70}")
    print(f"WALLET: {addr} ({label})")
    print(f"{'='*70}")
    print(f"Total txs fetched: {len(txs)}")
    if not txs: return
    
    oldest = min(txs, key=lambda x: x.get('timestamp',9e18))
    newest = max(txs, key=lambda x: x.get('timestamp',0))
    print(f"Date range: {ts(oldest.get('timestamp',0))} → {ts(newest.get('timestamp',0))} UTC")
    
    types = defaultdict(int)
    believe_net = 0.0
    sol_net = 0.0
    wsol_net = 0.0
    
    may3_txs = []
    
    for tx in txs:
        types[tx.get('type','?')] += 1
        t_val = tx.get('timestamp',0)
        
        for tt in (tx.get('tokenTransfers',[]) or []):
            mint = tt.get('mint','')
            amt  = float(tt.get('tokenAmount') or 0)
            frm  = tt.get('fromUserAccount','') or ''
            to   = tt.get('toUserAccount','')   or ''
            if mint == MINT:
                if to == addr: believe_net += amt
                if frm == addr: believe_net -= amt
            elif mint == WSOL:
                if to == addr: wsol_net += amt
                if frm == addr: wsol_net -= amt
        
        for nt in (tx.get('nativeTransfers',[]) or []):
            amt = (nt.get('amount') or 0) / 1e9
            frm = nt.get('fromUserAccount','') or ''
            to  = nt.get('toUserAccount','')   or ''
            if to == addr: sol_net += amt
            if frm == addr: sol_net -= amt
        
        # Flag May 3 transactions
        if t_val and 1777820000 <= t_val <= 1777900000:  # ~May 3 range
            may3_txs.append(tx)
    
    print(f"\nTx types: {dict(types)}")
    print(f"BELIEVE net: {believe_net:+,.2f}")
    print(f"WSOL net: {wsol_net:+.4f}")
    print(f"SOL net: {sol_net:+.4f}")
    
    # Recent BELIEVE transfers in/out
    print(f"\nFirst 10 transactions (oldest):")
    for tx in sorted(txs, key=lambda x: x.get('timestamp',0))[:10]:
        t_val = tx.get('timestamp',0)
        b_flow = 0.0
        for tt in (tx.get('tokenTransfers',[]) or []):
            if tt.get('mint') == MINT:
                amt = float(tt.get('tokenAmount') or 0)
                if tt.get('toUserAccount','') == addr: b_flow += amt
                if tt.get('fromUserAccount','') == addr: b_flow -= amt
        print(f"  {ts(t_val)} | {tx.get('type','?'):10} | BELIEVE {b_flow:+,.0f} | sig={tx.get('signature','?')[:40]}")

# ── Fetch Bs6cTDM6 (BELIEVE recipient from GCvHh buy) ─────────────────────
BS6 = "Bs6cTDM6Ac6WfenqEG4mNp156b7ZYGMcTXzo2MoZxtg5"
print(f"Fetching Bs6cTDM6...")
bs6_txs = fetch_history(BS6, limit=100)
json.dump(bs6_txs, open(WORKDIR/"wallet_Bs6cTDM6_history.json","w"), indent=2)
summarize_wallet(BS6, bs6_txs, "BELIEVE recipient from GCvHh ignition buy")

# ── Fetch 62N5myBG (BELIEVE recipient from APVT transfer) ─────────────────
N62 = "62N5myBG3E2pB1AhLdDi9SFfLP78Y7jHNkLa4qt5NjLK"
print(f"\nFetching 62N5myBG...")
n62_txs = fetch_history(N62, limit=100)
json.dump(n62_txs, open(WORKDIR/"wallet_62N5myBG_history.json","w"), indent=2)
summarize_wallet(N62, n62_txs, "BELIEVE recipient from APVT transfer at ignition")

