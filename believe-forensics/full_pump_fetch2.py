#!/usr/bin/env python3
"""
BELIEVE Full Pump Fetch v2 — correct pagination from pool's own head.
Each pool pages from its own most recent sig backwards until we reach the pump.
"""

import json, time, datetime, requests
from pathlib import Path
from collections import defaultdict

WORKDIR = Path("/home/user/test-launch/believe-forensics")
KEY = "458aa311-b56e-4a35-b3ec-0ba21c33886b"
RPC_URL   = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
PARSE_URL = f"https://api.helius.xyz/v0/transactions/?api-key={KEY}"
ENH_URL   = f"https://api.helius.xyz/v0/addresses"

MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"
WSOL = "So11111111111111111111111111111111111111112"

ALL_POOLS = [
    "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF",
    "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7",
]

PUMP_START = int(datetime.datetime(2026,5,3,19,50,0).timestamp())
PUMP_END   = int(datetime.datetime(2026,5,4,2,0,0).timestamp())
PRE_START  = int(datetime.datetime(2026,4,26,0,0,0).timestamp())

def ts(t): return datetime.datetime.utcfromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else "?"

def rpc(method, params, retries=8):
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
    time.sleep(0.3)
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
            print(f"  parse_batch error: {e}")
            time.sleep(min(10*2**att, 60))
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

def extract_trade(tx, pool_set=None):
    if not tx: return None
    fp   = tx.get("feePayer","")
    ts_v = tx.get("timestamp",0)
    sig  = tx.get("signature","")
    if tx.get("transactionError"): return None

    tok_xfers = tx.get("tokenTransfers",[]) or []
    nat_xfers = tx.get("nativeTransfers",[]) or []

    believe_net = defaultdict(float)
    wsol_net    = defaultdict(float)
    sol_net     = defaultdict(float)

    for tt in tok_xfers:
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

    for nt in nat_xfers:
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

    return {
        "sig": sig, "ts": ts_v, "dt": ts(ts_v),
        "wallet": fp, "side": side,
        "believe_delta": round(fp_believe, 4),
        "sol_cost": round(sol_cost, 6),
        "type": tx.get("type",""), "source": tx.get("source",""),
    }

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1: Collect all sig hashes in pump window for each pool
# ──────────────────────────────────────────────────────────────────────────────

sigs_file = WORKDIR / "pump_sigs_v2.json"
if sigs_file.exists():
    all_sigs = json.loads(sigs_file.read_text())
    print(f"Loaded {len(all_sigs)} cached sigs")
else:
    all_sigs = {}

    for pool in ALL_POOLS:
        label = pool[:16]
        print(f"\n── Pool {label}... paging backwards from now ──")
        before = None
        page = 0
        pool_found = 0
        past_window = False

        while not past_window:
            page += 1
            batch = get_sigs(pool, before=before, limit=1000)
            if not batch:
                print(f"  P{page}: empty"); break

            newest_bt = batch[0].get("blockTime",0) or 0
            oldest_bt = batch[-1].get("blockTime",0) or 0

            in_win = 0
            for s in batch:
                bt  = s.get("blockTime",0) or 0
                sig = s.get("signature","")
                if bt < PUMP_START:
                    past_window = True
                if PUMP_START <= bt <= PUMP_END and not s.get("err") and sig:
                    if sig not in all_sigs:
                        all_sigs[sig] = {"pool": pool, "blockTime": bt, "slot": s.get("slot",0)}
                        in_win += 1
                        pool_found += 1

            print(f"  P{page}: {len(batch)} sigs {ts(oldest_bt)} → {ts(newest_bt)} | +{in_win} pump sigs (pool total={pool_found})")
            before = batch[-1]["signature"]

            if oldest_bt < PRE_START or len(batch) < 1000:
                break

    sigs_file.write_text(json.dumps(all_sigs, indent=2))
    print(f"\nSaved {len(all_sigs)} total pump window sigs")

print(f"\nTotal pump window sigs: {len(all_sigs)}")

# ──────────────────────────────────────────────────────────────────────────────
# STEP 2: Parse all sigs in batches of 100
# ──────────────────────────────────────────────────────────────────────────────

trades_file = WORKDIR / "pump_trades_v2.json"
if trades_file.exists():
    all_trades = json.loads(trades_file.read_text())
    print(f"Loaded {len(all_trades)} cached trades")
else:
    all_trades = []
    sig_list = sorted(all_sigs.keys(), key=lambda s: all_sigs[s].get("blockTime", 0))
    print(f"\nParsing {len(sig_list)} sigs in batches of 100...")

    for i in range(0, len(sig_list), 100):
        chunk = sig_list[i:i+100]
        parsed = parse_batch(chunk)
        n_trades = 0
        for tx in parsed:
            trade = extract_trade(tx)
            if trade:
                trade["pool"] = all_sigs.get(tx.get("signature",""), {}).get("pool","?")
                all_trades.append(trade)
                n_trades += 1
        print(f"  Batch {i//100+1}/{(len(sig_list)+99)//100}: parsed={len(parsed)}, trades={n_trades}")

    trades_file.write_text(json.dumps(all_trades, indent=2))
    print(f"Saved {len(all_trades)} trades")

# ──────────────────────────────────────────────────────────────────────────────
# STEP 3: Analysis
# ──────────────────────────────────────────────────────────────────────────────

all_trades.sort(key=lambda t: t.get("ts",0))
buys  = [t for t in all_trades if t["side"]=="buy"]
sells = [t for t in all_trades if t["side"]=="sell"]

print(f"\n{'='*70}")
print(f"PUMP WINDOW SUMMARY  ({ts(PUMP_START)} → {ts(PUMP_END)} UTC)")
print(f"{'='*70}")
print(f"  Total trades: {len(all_trades)}  (buys={len(buys)}, sells={len(sells)})")
total_sol_in  = sum(t["sol_cost"] for t in buys)
total_sol_out = sum(t["sol_cost"] for t in sells)
print(f"  Total SOL in (buys):  {total_sol_in:.2f} SOL")
print(f"  Total SOL out (sells): {total_sol_out:.2f} SOL")

print(f"\n{'=FIRST 30 BUYS (ignition candidates)=':^70}")
print(f"{'Time UTC':10}{'BELIEVE':>14}{'SOL':>10}{'Pool':>14}  {'Wallet'}")
print("-"*100)
for t in buys[:30]:
    print(f"  {t['dt'][-8:]}  {t['believe_delta']:>12,.2f}  {t['sol_cost']:>8.4f}  {t['pool'][:14]:>14}  {t['wallet']}")

# Unique buyers
buyer_map = defaultdict(lambda: {"count":0,"believe":0.0,"sol":0.0,"first_ts":9e18,"first_dt":"?"})
for t in buys:
    w = t["wallet"]
    buyer_map[w]["count"]   += 1
    buyer_map[w]["believe"] += t["believe_delta"]
    buyer_map[w]["sol"]     += t["sol_cost"]
    if t["ts"] < buyer_map[w]["first_ts"]:
        buyer_map[w]["first_ts"] = t["ts"]
        buyer_map[w]["first_dt"] = t["dt"]

buyers_sorted = sorted(buyer_map.items(), key=lambda x: x[1]["first_ts"])
print(f"\n{'=UNIQUE BUYERS (chronological, first 30)=':^70}")
print(f"{'First Buy':11}{'Count':>6}{'BELIEVE':>14}{'SOL':>10}  {'Wallet'}")
print("-"*90)
for w, info in buyers_sorted[:30]:
    print(f"  {info['first_dt'][-8:]}  {info['count']:>5}  {info['believe']:>12,.2f}  {info['sol']:>8.4f}  {w}")

buyer_list = [{"wallet":w, "first_dt":v["first_dt"], "first_ts":int(v["first_ts"]),
               "buy_count":v["count"], "believe_bought":round(v["believe"],2),
               "sol_spent":round(v["sol"],4)}
              for w,v in buyers_sorted]
(WORKDIR/"pump_buyers_v2.json").write_text(json.dumps(buyer_list, indent=2))

ignition = [b["wallet"] for b in buyer_list[:10]]
(WORKDIR/"ignition_wallets.json").write_text(json.dumps({"ignition_wallets": ignition}, indent=2))
print(f"\n  Ignition wallets (first 10): {ignition[:3]}...")
print("\nDone.")
