#!/usr/bin/env python3
"""
Scan POOL_MAIN sigs from 19:50-21:00 UTC May 3 (first pump hour).
Uses the 21:26 UTC anchor sigs from pump_txs_enhanced and pages backwards.
"""
import json, time, requests, datetime
from pathlib import Path
from collections import defaultdict

WORKDIR = Path("/home/user/test-launch/believe-forensics")
KEY = "458aa311-b56e-4a35-b3ec-0ba21c33886b"
RPC_URL   = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
PARSE_URL = f"https://api.helius.xyz/v0/transactions/?api-key={KEY}"
ENH_URL   = f"https://api.helius.xyz/v0/addresses"

POOL_MAIN = "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF"
POOL2     = "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7"
MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"
WSOL = "So11111111111111111111111111111111111111112"

HOUR1_START = int(datetime.datetime(2026,5,3,19,50,0).timestamp())  # 19:50 UTC
HOUR1_END   = int(datetime.datetime(2026,5,3,21,0,0).timestamp())   # 21:00 UTC

def ts(t): return datetime.datetime.utcfromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else "?"

def rpc(method, params, retries=6):
    pay = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    for att in range(retries):
        try:
            r = requests.post(RPC_URL, json=pay, timeout=30)
            if r.status_code == 429:
                w = min(8*2**att, 120); print(f"  RPC 429 wait {w}s"); time.sleep(w); continue
            r.raise_for_status()
            d = r.json()
            return None if "error" in d else d.get("result")
        except Exception as e:
            time.sleep(min(4*2**att, 60))
    return None

def get_sigs(addr, before=None, limit=1000):
    p = [addr, {"limit": limit, "commitment": "finalized"}]
    if before: p[1]["before"] = before
    res = rpc("getSignaturesForAddress", p)
    time.sleep(0.35)
    return res or []

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

def extract_trade(tx):
    if not tx: return None
    fp   = tx.get("feePayer","")
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

    fp_believe = believe_net.get(fp, 0)
    fp_wsol    = wsol_net.get(fp, 0)
    fp_sol     = sol_net.get(fp, 0)

    if abs(fp_believe) < 0.001: return None
    side = "buy" if fp_believe > 0 else "sell"
    sol_cost = abs(fp_sol + fp_wsol)
    
    # Also track the actual BELIEVE receiver (for Jupiter routes)
    max_recv = max(believe_net.items(), key=lambda x: x[1]) if believe_net else ("", 0)
    
    return {
        "sig": sig, "ts": ts_v, "dt": ts(ts_v),
        "wallet": fp, "side": side,
        "believe_delta": round(fp_believe, 4),
        "sol_cost": round(sol_cost, 6),
        "believe_receiver": max_recv[0] if max_recv[1] > 1 else fp,
        "type": tx.get("type",""), "source": tx.get("source",""),
    }

# ─── Phase 1: Collect sigs in first hour window ───────────────────────────

sigs_file = WORKDIR / "first_hour_sigs.json"
if sigs_file.exists():
    window_sigs = json.loads(sigs_file.read_text())
    print(f"Loaded {len(window_sigs)} cached first-hour sigs")
else:
    # Use the newest known pool sig as anchor
    # From pump_txs_enhanced: sig at 21:26:13 UTC
    ANCHOR_SIG = "3fBiYMaRVr7CN3LrRvRWiYogR5MTNgf95s3vVQqbFM6MT1rkbg2LWpWCa1N4"
    
    window_sigs = {}
    before = ANCHOR_SIG
    page = 0
    past_window = False
    
    print(f"Fetching POOL_MAIN sigs backwards from 21:26 UTC May 3...")
    print(f"Target window: {ts(HOUR1_START)} → {ts(HOUR1_END)} UTC\n")
    
    while not past_window and page < 15:
        page += 1
        batch = get_sigs(POOL_MAIN, before=before, limit=1000)
        if not batch:
            print(f"  P{page}: empty, stopping")
            break
        
        newest_bt = batch[0].get("blockTime",0) or 0
        oldest_bt = batch[-1].get("blockTime",0) or 0
        in_win = 0
        
        for s in batch:
            bt  = s.get("blockTime",0) or 0
            sig = s.get("signature","")
            if bt < HOUR1_START:
                past_window = True
            if HOUR1_START <= bt <= HOUR1_END and not s.get("err") and sig:
                if sig not in window_sigs:
                    window_sigs[sig] = {"blockTime": bt, "slot": s.get("slot",0)}
                    in_win += 1
        
        print(f"  P{page}: {len(batch)} sigs | {ts(oldest_bt)}→{ts(newest_bt)} | +{in_win} in window (total={len(window_sigs)})")
        before = batch[-1]["signature"]
        
        if len(batch) < 1000 or oldest_bt < HOUR1_START - 3600:
            break
    
    sigs_file.write_text(json.dumps(window_sigs, indent=2))
    print(f"\nSaved {len(window_sigs)} first-hour sigs")

print(f"\nTotal first-hour sigs: {len(window_sigs)}")

# ─── Phase 2: Batch parse ─────────────────────────────────────────────────

trades_file = WORKDIR / "first_hour_trades.json"
if trades_file.exists():
    all_trades = json.loads(trades_file.read_text())
    print(f"Loaded {len(all_trades)} cached first-hour trades")
else:
    all_trades = []
    sig_list = sorted(window_sigs.keys(), key=lambda s: window_sigs[s].get("blockTime", 0))
    print(f"Parsing {len(sig_list)} sigs in batches of 100...")
    
    for i in range(0, len(sig_list), 100):
        chunk = sig_list[i:i+100]
        parsed = parse_batch(chunk)
        n_trades = 0
        for tx in parsed:
            trade = extract_trade(tx)
            if trade:
                all_trades.append(trade)
                n_trades += 1
        print(f"  Batch {i//100+1}/{(len(sig_list)+99)//100}: parsed={len(parsed)}, trades={n_trades}")
    
    trades_file.write_text(json.dumps(all_trades, indent=2))
    print(f"Saved {len(all_trades)} trades")

# ─── Phase 3: Analysis ────────────────────────────────────────────────────

all_trades.sort(key=lambda t: t.get("ts",0))
buys  = [t for t in all_trades if t["side"]=="buy"]
sells = [t for t in all_trades if t["side"]=="sell"]

print(f"\n{'='*70}")
print(f"FIRST-HOUR SUMMARY  ({ts(HOUR1_START)} → {ts(HOUR1_END)} UTC)")
print(f"{'='*70}")
print(f"  Total trades: {len(all_trades)}  (buys={len(buys)}, sells={len(sells)})")
total_sol_in  = sum(t["sol_cost"] for t in buys)
total_sol_out = sum(t["sol_cost"] for t in sells)
print(f"  Total SOL in (buys):  {total_sol_in:.2f} SOL")
print(f"  Total SOL out (sells): {total_sol_out:.2f} SOL")

print(f"\n{'=FIRST 20 BUYS (ignition sequence)=':^70}")
print(f"{'Time UTC':10}{'BELIEVE':>14}{'SOL':>10}  {'Wallet'}")
print("-"*90)
for t in buys[:20]:
    print(f"  {t['dt'][-8:]}  {t['believe_delta']:>12,.0f}  {t['sol_cost']:>8.4f}  {t['wallet']}")

# Top 20 unique buyers
buyer_map = defaultdict(lambda: {"count":0,"believe":0.0,"sol":0.0,"first_ts":9e18,"first_dt":"?"})
for t in buys:
    w = t["wallet"]
    buyer_map[w]["count"] += 1
    buyer_map[w]["believe"] += t["believe_delta"]
    buyer_map[w]["sol"] += t["sol_cost"]
    if t["ts"] < buyer_map[w]["first_ts"]:
        buyer_map[w]["first_ts"] = t["ts"]
        buyer_map[w]["first_dt"] = t["dt"]

buyers_sorted = sorted(buyer_map.items(), key=lambda x: x[1]["sol"], reverse=True)
print(f"\n{'=TOP 20 BUYERS BY SOL SPENT=':^70}")
print(f"{'First Buy':10}{'Count':>6}{'BELIEVE':>14}{'SOL':>10}  {'Wallet'}")
print("-"*90)
for w, info in buyers_sorted[:20]:
    print(f"  {info['first_dt'][-8:]}  {info['count']:>5}  {info['believe']:>12,.0f}  {info['sol']:>8.4f}  {w}")

# Save
buyer_list = [{"wallet":w, "first_dt":v["first_dt"], "first_ts":int(v["first_ts"]),
               "buy_count":v["count"], "believe_bought":round(v["believe"],2),
               "sol_spent":round(v["sol"],4)}
              for w,v in buyers_sorted]
(WORKDIR/"first_hour_buyers.json").write_text(json.dumps(buyer_list, indent=2))
print(f"\nSaved {len(buyer_list)} unique buyers to first_hour_buyers.json")
