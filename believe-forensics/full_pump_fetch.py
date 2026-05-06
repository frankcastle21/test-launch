#!/usr/bin/env python3
"""
BELIEVE Full Pump Fetch — uses Helius RPC + batch parse across all pools.
Anchors from known slot near pump end, pages backwards to pump start.
"""

import json, time, datetime, requests
from pathlib import Path
from collections import defaultdict

WORKDIR = Path("/home/user/test-launch/believe-forensics")
KEY = "458aa311-b56e-4a35-b3ec-0ba21c33886b"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
PARSE_URL = f"https://api.helius.xyz/v0/transactions/?api-key={KEY}"
ENH_URL   = f"https://api.helius.xyz/v0/addresses"

MINT = "BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf"

# All known BELIEVE Meteora pool addresses (from DexScreener)
ALL_POOLS = [
    "BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF",  # main, created Oct 15 2025
    "FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7",  # pool2, created Oct 15 2025
    "DXwjTJm7zshc1RsWDspU59WVtdrjj1Unxrz2KhQ7tW11",  # created Feb 2026
    "C6qjXCe9WnSPBgRX7cCzdHDF5qRnzxnXeN8q9eNK1hht",  # created Feb 2026
    "2kqVuDrFhCEphkGqFhCgi13Dz28H6uHSwJbTh8NzEKF7",  # created Feb 2026
    "AgBKL6Qf2FeECWdCYbikh8kabEJrjptTUi4ZnXnwAqBt",  # created Oct 2025
]

# Pump window
PUMP_START = int(datetime.datetime(2026,5,3,19,50,0).timestamp())  # 19:50 UTC May 3
PUMP_END   = int(datetime.datetime(2026,5,4,2,0,0).timestamp())    # 02:00 UTC May 4

# Reference sig at 21:26 UTC May 3 (from earlier binary search)
REF_SIG_21_26 = "2cErc9WC6AtuoM7v2yVDAobqcgQdQ6yu1yJb3YMbrUgtimfNQ5v8dV4JFHw9fwMJkXhAKZ7N4JGKnQipuJzjD4Nt5"

def ts(t): return datetime.datetime.utcfromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else "?"

def rpc(method, params, retries=6):
    pay = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    for att in range(retries):
        try:
            r = requests.post(RPC_URL, json=pay, timeout=30)
            if r.status_code == 429:
                w = min(4*2**att,60); print(f"  RPC 429 →{w}s"); time.sleep(w); continue
            r.raise_for_status()
            d = r.json()
            return None if "error" in d else d.get("result")
        except Exception as e:
            time.sleep(min(3*2**att,30))
    return None

def get_sigs(addr, before=None, limit=1000):
    p = [addr, {"limit":limit,"commitment":"finalized"}]
    if before: p[1]["before"] = before
    res = rpc("getSignaturesForAddress", p)
    time.sleep(0.25)
    return res or []

def parse_batch(sigs):
    """Helius batch parse — up to 100 sigs at once."""
    try:
        r = requests.post(PARSE_URL, json={"transactions": sigs}, timeout=60)
        if r.status_code == 429:
            time.sleep(10); r = requests.post(PARSE_URL, json={"transactions": sigs}, timeout=60)
        r.raise_for_status()
        time.sleep(0.5)
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        print(f"  parse_batch error: {e}")
        return []

def helius_enh(addr, before=None, limit=100):
    url = f"{ENH_URL}/{addr}/transactions"
    params = {"api-key": KEY, "limit": limit}
    if before: params["before"] = before
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(10); r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        time.sleep(0.4)
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception as e:
        print(f"  helius_enh error: {e}")
        return []

def extract_trade(tx, pool_addrs):
    """Extract buy/sell from parsed Helius tx. Handles multi-hop Jupiter routes."""
    if not tx: return None
    fp   = tx.get("feePayer","")
    ts_v = tx.get("timestamp",0)
    sig  = tx.get("signature","")
    err  = tx.get("transactionError")
    if err: return None

    tok_xfers = tx.get("tokenTransfers",[]) or []
    nat_xfers = tx.get("nativeTransfers",[]) or []

    believe_net = defaultdict(float)   # addr → net BELIEVE tokens
    wsol_net    = defaultdict(float)   # addr → net WSOL
    sol_net     = defaultdict(float)   # addr → net SOL (native)

    for tt in tok_xfers:
        mint = tt.get("mint","")
        amt  = float(tt.get("tokenAmount") or 0)
        frm  = tt.get("fromUserAccount","") or ""
        to   = tt.get("toUserAccount","")   or ""
        if mint == MINT:
            believe_net[frm] -= amt
            believe_net[to]  += amt
        elif mint == "So11111111111111111111111111111111111111112":  # WSOL
            wsol_net[frm] -= amt
            wsol_net[to]  += amt

    for nt in nat_xfers:
        amt = (nt.get("amount") or 0) / 1e9
        frm = nt.get("fromUserAccount","") or ""
        to  = nt.get("toUserAccount","")   or ""
        sol_net[frm] -= amt
        sol_net[to]  += amt

    # Only care if there's a net BELIEVE change for fee_payer (= real trade)
    fp_believe = believe_net.get(fp, 0)
    fp_wsol    = wsol_net.get(fp, 0)
    fp_sol     = sol_net.get(fp, 0)

    if abs(fp_believe) < 0.001:
        return None  # no BELIEVE movement for this wallet — skip

    side = "buy" if fp_believe > 0 else "sell"
    # Cost in SOL = native + wsol spent
    sol_spent = abs(fp_sol + fp_wsol) if side=="buy" else (fp_sol + fp_wsol)

    return {
        "sig": sig,
        "ts": ts_v,
        "dt": ts(ts_v),
        "wallet": fp,
        "side": side,
        "believe_delta": round(fp_believe, 4),
        "sol_cost": round(sol_spent, 6),
        "type": tx.get("type",""),
        "source": tx.get("source",""),
    }

# ─── MAIN FETCH LOOP per pool ──────────────────────────────────────────────────

all_sigs_file = WORKDIR / "all_pump_sigs.json"
if all_sigs_file.exists():
    all_sigs = json.loads(all_sigs_file.read_text())
    print(f"Loaded {len(all_sigs)} cached sigs")
else:
    all_sigs = {}   # sig → {pool, blockTime, ...}

    for pool in ALL_POOLS:
        label = pool[:12]
        print(f"\n── Pool {label}... fetching sigs around pump window ──")

        # Start from REF_SIG (21:26 UTC May 3) or from current if no ref
        before_cursor = REF_SIG_21_26
        page = 0
        past = False

        while not past:
            page += 1
            batch = get_sigs(pool, before=before_cursor, limit=1000)
            if not batch:
                print(f"  P{page}: empty, stopping"); break

            newest_bt = batch[0].get("blockTime",0)
            oldest_bt = batch[-1].get("blockTime",0)

            in_win = 0
            for s in batch:
                bt = s.get("blockTime",0) or 0
                sig = s.get("signature","")
                if bt < PUMP_START:
                    past = True
                    break
                if PUMP_START <= bt <= PUMP_END and not s.get("err") and sig:
                    if sig not in all_sigs:
                        all_sigs[sig] = {"pool":pool,"blockTime":bt,"slot":s.get("slot")}
                        in_win += 1

            print(f"  P{page}: {len(batch)} sigs {ts(oldest_bt)}→{ts(newest_bt)}, +{in_win} new (total={len(all_sigs)})")
            before_cursor = batch[-1]["signature"]

            if len(batch) < 1000:
                print(f"  Last page, stopping"); break

    all_sigs_file.write_text(json.dumps(all_sigs, indent=2))
    print(f"\nSaved {len(all_sigs)} total sigs across all pools")

# ─── BATCH PARSE ──────────────────────────────────────────────────────────────

trades_file = WORKDIR / "all_pump_trades.json"
if trades_file.exists():
    all_trades = json.loads(trades_file.read_text())
    print(f"\nLoaded {len(all_trades)} cached parsed trades")
else:
    all_trades = []
    parsed_sigs = set()

    sig_list = sorted(all_sigs.keys(), key=lambda s: all_sigs[s].get("blockTime",0))
    print(f"\nParsing {len(sig_list)} sigs in batches of 100...")

    for i in range(0, len(sig_list), 100):
        chunk = sig_list[i:i+100]
        parsed = parse_batch(chunk)
        print(f"  Batch {i//100+1}: got {len(parsed)} parsed txs")

        for tx in parsed:
            sig = tx.get("signature","")
            parsed_sigs.add(sig)
            pool = all_sigs.get(sig,{}).get("pool","?")
            pool_set = set(ALL_POOLS)
            trade = extract_trade(tx, pool_set)
            if trade:
                trade["pool"] = pool
                all_trades.append(trade)

    trades_file.write_text(json.dumps(all_trades, indent=2))
    print(f"Saved {len(all_trades)} trades")

# ─── ANALYSIS ─────────────────────────────────────────────────────────────────

all_trades.sort(key=lambda t: t.get("ts",0))
buys  = [t for t in all_trades if t["side"]=="buy"]
sells = [t for t in all_trades if t["side"]=="sell"]

print(f"\n{'='*70}")
print(f"PUMP WINDOW TRADE SUMMARY  (May 3 19:50 → May 4 02:00 UTC)")
print(f"{'='*70}")
print(f"  Total txs parsed: {len(all_trades)}  (buys={len(buys)}, sells={len(sells)})")

if buys:
    print(f"\n{'=FIRST 30 BUYS (ignition candidates)=':^70}")
    print(f"{'Time UTC':11}{'BELIEVE':>14}{'SOL':>10}{'Pool':>14}  {'Wallet'}")
    print("-"*100)
    for t in buys[:30]:
        print(f"  {t['dt'][-8:]}  {t['believe_delta']:>12,.0f}  {t['sol_cost']:>8.4f}  {t['pool'][:12]:>12}  {t['wallet']}")

# Unique buyers summary
buyer_map = defaultdict(lambda: {"count":0,"believe":0,"sol":0,"first_ts":9e18,"first_dt":"?"})
for t in buys:
    w = t["wallet"]
    buyer_map[w]["count"]   += 1
    buyer_map[w]["believe"] += t["believe_delta"]
    buyer_map[w]["sol"]     += t["sol_cost"]
    if t["ts"] < buyer_map[w]["first_ts"]:
        buyer_map[w]["first_ts"] = t["ts"]
        buyer_map[w]["first_dt"] = t["dt"]

buyers_sorted = sorted(buyer_map.items(), key=lambda x: x[1]["first_ts"])
print(f"\n{'=UNIQUE BUYERS (chronological)=':^70}")
print(f"{'First Buy':20}{'Count':>6}{'BELIEVE':>14}{'SOL':>10}  {'Wallet'}")
print("-"*90)
for w,info in buyers_sorted[:30]:
    print(f"  {info['first_dt'][-8:]}  {info['count']:>5}  {info['believe']:>12,.0f}  {info['sol']:>8.4f}  {w}")

# Save buyer list
buyer_list = [{"wallet":w,"first_dt":v["first_dt"],"first_ts":v["first_ts"],
               "buy_count":v["count"],"believe_bought":v["believe"],"sol_spent":v["sol"]}
              for w,v in buyers_sorted]
(WORKDIR/"pump_buyers.json").write_text(json.dumps(buyer_list, indent=2))

# Ignition wallets = first 10 buyers
ignition = [b["wallet"] for b in buyer_list[:10]]
(WORKDIR/"ignition_wallets.json").write_text(json.dumps({"ignition_wallets":ignition}, indent=2))
print(f"\nIgnition wallets (first 10):")
for w in ignition: print(f"  {w}")

print("\nDone.")
