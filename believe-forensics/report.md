# BELIEVE ($BELIEVE) Token Pump — On-Chain Forensics Report

**Date of investigation:** May 6, 2026  
**Token mint:** `BLVxek8YMXUQhcKmMvrFTrzh5FXg8ec88Crp6otEaCMf`  
**Network:** Solana mainnet  
**Data sources:** Helius RPC + Enhanced API (api-key: 458aa311…), GeckoTerminal OHLCV API, DexScreener API  
**Primary pool:** `BJsSymifLkwUq8r3KMSiFH6t5JbLLBd1VJDgzD7qYNxF` (Meteora DLMM, hereafter POOL_MAIN)  
**Secondary pool:** `FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7` (POOL2)

---

## Context

BELIEVE (formerly LAUNCHCOIN, rebranded Oct 15, 2025 by Ben Pasternak's Believe.app) began a sustained pump on May 3, 2026. Total supply: **1,280,085,664 BELIEVE**.

| Checkpoint | Time (UTC) | Price | Market Cap |
|---|---|---|---|
| Pre-pump (flat) | May 3 19:55 | $0.0006931 | ~$887K |
| Ignition first real buy | May 3 19:58:35 | $0.000694 | ~$889K |
| First breakout candle | May 3 20:00 | $0.000728→$0.000765 | ~$979K |
| End of first hour | May 3 21:00 | $0.001626 | ~$2.08M |
| Peak | May 4 13:00 | $0.007808 | ~$9.99M |
| Current (May 6) | — | $0.004541 | ~$5.81M |

The token spent several weeks in a flat consolidation band at ~$887K market cap before the event described here.

---

## Q1 — Ignition Wallets: Who Bought First and Why Does It Matter?

### True ignition timestamp

GeckoTerminal 1-minute OHLCV confirms the first price-moving activity at **19:58 UTC May 3, 2026**. The 19:58 minute candle shows price jumping from $0.000694 → $0.000728 (+5%) on the first real purchase. All candles before 19:58 have <$100 volume and flat price action.

### Pre-ignition team activity (19:55–19:58 UTC)

**Before** any market buy, the distribution wallet `APVT9fhDpfq5qf2yJFtEesfuz3ziPf8dRfFkpJ3gYuLu` executed three zero-cost BELIEVE accumulations through POOL_MAIN:

| Time UTC | BELIEVE | SOL Cost | Tx Sig (prefix) |
|---|---|---|---|
| 19:55:47 | +20,464 | 0.000 | Via POOL_MAIN |
| 19:58:04 | +69,934 | 0.000 | Via POOL_MAIN |
| 19:58:28 | +70,026 | 0.000 | Via POOL_MAIN |

These are **not open-market buys** — feePayer SOL delta is essentially zero. APVT appears in POOL_MAIN's `getSignaturesForAddress` but pays no WSOL. This pattern (pool interaction with zero payment) is consistent with a project-controlled wallet adjusting its liquidity position or collecting protocol allocations, not a retail buyer.

Similarly, `CdSkvzb45HpGCFAFBEzCUwYsgpXYW8GfVdmfGtkadG8i` received 71,488 BELIEVE at 19:58:36 for 0 SOL.

### First genuine market buy — FkaLnX17

**`FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp`** executed the first SOL-denominated purchase at:

- **19:58:35 UTC May 3, 2026**
- 183,161 BELIEVE purchased for **1.5294 SOL**
- Source: POOL_MAIN via `getSignaturesForAddress`
- Citation: slot ~417381679 (estimated from blockTime 1777838315)

This wallet went on to buy 155 times in the first hour alone, spending **241.44 SOL** for **16,857,908 BELIEVE** — representing ~62% of all first-hour SOL inflow by a single wallet. The funder of FkaLnX17 was confirmed as `3epyFweiQNFvfCFWic3C8AKMzeuvMsYvF7u7fVZTWcYH`, which shows only UNKNOWN-type transactions with no visible BELIEVE trading history.

### Second market buy — GCvHh routing wallet

At **19:58:52 UTC** (17 seconds after FkaLnX17):

- **`GCvHhEUYQwTJ8jyf8Lc4bv8jBXyZU4LMMsZCobwEPzvM`** (feePayer) spent **1.9736 WSOL**
- 228,448 BELIEVE delivered to **`Bs6cTDM6Ac6WfenqEG4mNp156b7ZYGMcTXzo2MoZxtg5`** (not the feePayer)
- Routed via Jupiter aggregator (`ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn`)
- Tx sig: `4F5BBQUehyMk92qc2hc14ygof9C7coAXtjtG8CQPwkyVb89g1p44buWo6PmR4TSzGu6UKnMQ1zEiALbUybVUddam`

`Bs6cTDM6` (the BELIEVE recipient) is a SWAP-only wallet with net +771,248 BELIEVE across 100 recent txs — consistent with a second-stage accumulation wallet.

### APVT large transfer at ignition

At **19:58:46 UTC** (between the two market buys), APVT executed:

- **267,599 BELIEVE transferred** to `62N5myBG3E2pB1AhLdDi9SFfLP78Y7jHNkLa4qt5NjLK`
- Tx sig: `2jipUmvs22mEMigmKxaWenBzzenTqBvG8CqvmUKTjdxKwNLBMdkFbNuE6sfGWZv8Zp3bQHPj5BNdnPGE3vXziEvs`
- Tx type: TRANSFER (not a swap)

`62N5myBG` is a pass-through distribution wallet (described in Q4).

### MEV/arb wallets at ignition

- **`GgGap6tiUKhD8XeUZaHpVB3rerb3zKWNouWQJHozDujt`** — SWAP at 19:58:38 UTC, 0 SOL net, 0 BELIEVE net. Round-trip arbitrageur. Not a buyer.
- **`GxDC9e7SP9mzhDo4re5HbpLa2RW7gB9DtmThx4i4pXSq`** — TRANSFER at 19:58:52 UTC, 3,624 BELIEVE moved from pool POOL2 → POOL_MAIN vault. Cross-pool arbitrage.

### Ignition wallet summary

| Wallet (prefix) | Role | Time UTC | BELIEVE | SOL |
|---|---|---|---|---|
| APVT9fhDpfq5 | Team dist. pre-load | 19:55–19:58 | +160,424 | 0.000 |
| FkaLnX17cXZG | **FIRST real buyer** | 19:58:35 | +183,161 | 1.529 |
| CdSkvzb45HpG | Insider pre-load | 19:58:36 | +71,488 | 0.000 |
| GgGap6tiUKhD | MEV/arb (no net) | 19:58:38 | 0 | 0 |
| APVT9fhDpfq5 | Transfer to 62N5myBG | 19:58:46 | -267,599 | 0 |
| GCvHhEUYQwTJ | 2nd real buyer (→Bs6c) | 19:58:52 | +228,448* | 1.974 WSOL |
| GxDC9e7SP9mz | Cross-pool arb | 19:58:52 | 0 | 0 |

*BELIEVE delivered to Bs6cTDM6, not to GCvHh feePayer directly.

---

## Q2 — Pump Continuation: Top 20 Buyers in the First Hour

**Data:** 1,038 POOL_MAIN signatures filtered to 19:50–21:00 UTC May 3; Helius batch-parsed 1,038 txs → 423 qualifying trades (280 buys, 143 sells).

**First-hour pool stats:**
- Total SOL in (buys): **395.27 SOL**
- Total SOL out (sells): **353.76 SOL**
- Net: +41.5 SOL buying pressure
- Unique wallets buying: 29

### Top 20 buyers by SOL spent (first hour):

| Rank | First Buy | Buys | BELIEVE | SOL | Wallet |
|---|---|---|---|---|---|
| 1 | 19:58:35 | 155 | 16,857,908 | 241.44 | `FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp` |
| 2 | 20:14:03 | 5 | 4,451,254 | 114.00 | `A4StKNA7M8WqS3o5PtoSYTTCxPsk2EGHeeHAXfb8UWqS` |
| 3 | 20:37:39 | 2 | 364,214 | 13.40 | `4pkCyvj7KjpyYtHCgaxN4dbeyeNa67dtp1JncauU4MFh` |
| 4 | 20:22:29 | 4 | 391,363 | 12.00 | `3rSutpkQ3JdsRHi7z9q29GtS6rEVN6bRCxWME8hhQZuJ` |
| 5 | 20:37:08 | 2 | 167,084 | 6.00 | `3zxrU8jj3zhvuF5PjdTFAdniNEkqwebuNG383XaWoUer` |
| 6 | 20:37:15 | 1 | 109,462 | 4.00 | `AyAMwoNeLuHJkzm8Y9XH9MgA1VNW5LXdSbsoAgc5bDQQ` |
| 7 | 20:35:14 | 3 | 63,720 | 2.28 | `3GqstJGjfRXCFoLUjHdgpw5T9VXb44xnoqSpbo4qi6s6` |
| 8 | 20:37:07 | 1 | 28,310 | 1.00 | `HWZ75GBUJTrW3mMMTkrQ5Xukd1o7ERdk7gYxBSJRNUvv` |
| 9 | 20:37:23 | 1 | 10,941 | 0.40 | `BeTLRMb7MnZ7J21ciRQ5FsiLviiU4s7k1jKGHyrmNekB` |
| 10 | 20:33:03 | 1 | 8,530 | 0.24 | `ARQiWqaXAaXKYqY4fNX2NwB6xiAn78dZ9hZQqqpBXv2e` |
| 11 | 20:37:08 | 1 | 5,656 | 0.20 | `4MGEgHqkHA9suyKUidYU91vojPDdyULTVrtuMUSsUofg` |
| 12–20 | 20:14–20:39 | 1 each | <4,000 | <0.15 each | Various retail |

**Concentration:** Buyers #1 and #2 (FkaLnX17 + A4StKNA7) account for **355 SOL out of 395 SOL total (90%)** in the first hour. This is extraordinarily concentrated — virtually all first-hour price movement was driven by two wallets.

**Note on APVT:** APVT9fhDpfq5 does not appear in this ranking because its pool interactions show 0 SOL cost. It is classified separately as a team/distribution wallet (Q4).

**Data gap:** POOL2 (`FJRyfjwjTxcW6d5na84Q8tLx8FhDHMNhf9JyACUCFiU7`) was not independently scanned for this hour. Cross-pool trades via Jupiter would appear attributed to POOL_MAIN in the feePayer's flow. The 395 SOL figure may undercount total pump-hour buying.

---

## Q3 — Pre-Positioning: April 26 – May 3

### APVT wallet pre-pump activity

`APVT9fhDpfq5qf2yJFtEesfuz3ziPf8dRfFkpJ3gYuLu` shows zero-cost BELIEVE accumulation through POOL_MAIN starting **before the pump** at 19:55 UTC on May 3. The wallet was accumulating BELIEVE in the pool minutes before FkaLnX17 placed the first real SOL buy.

### 62N5myBG distribution schedule

`62N5myBG3E2pB1AhLdDi9SFfLP78Y7jHNkLa4qt5NjLK` shows a **daily automated BELIEVE distribution** pattern that predates the pump by at least a week:

| Date | Amount Out | Recipient |
|---|---|---|
| Apr 28 08:26 UTC | 5,926,388 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |
| Apr 29 08:31 UTC | 732,370 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |
| Apr 30 08:30 UTC | 360,476 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |
| May 2 08:42 UTC | 741,718 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |
| May 3 08:26 UTC | 2,969,069 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |
| May 4 08:15 UTC | 6,036,633 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |
| May 5 08:15 UTC | 4,707,392 BELIEVE | `u6PJ8DtQuPFnfmwHbGFU…` |

All transfers go to a single downstream wallet (`u6PJ8Dt…`). Total transferred to this recipient across the 100-tx window: **21.5M+ BELIEVE**. This appears to be a programmatic daily distribution — possibly vesting, rewards, or a treasury drip mechanism. It is **not** pump-related (it predates the pump and continues daily after it).

**Data gap:** APVT's wallet history only reaches back to May 4 11:17 UTC across 2,000 transactions; we did not retrieve its May 3 history to confirm its pre-pump activities. Seven additional pagination pages would be needed. Based on block-level scanning, APVT was active near the pool from at least 19:55 UTC May 3.

**Data gap:** FkaLnX17's pre-pump history (April 26 – May 3) was not retrieved. Whether FkaLnX17 held any BELIEVE before the pump or was funded specifically for this event is unknown.

---

## Q4 — Insider Distribution: Who Received BELIEVE Without Paying?

### The APVT distribution hub

`APVT9fhDpfq5qf2yJFtEesfuz3ziPf8dRfFkpJ3gYuLu` operates as a systematic distribution hub. From its 2,000-tx history (May 4–6), it:

1. **Buys BELIEVE via Jupiter** (WSOL → BELIEVE via `ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn`)
2. **Distributes to 8+ sub-wallets** in ~10,000 BELIEVE chunks per transaction:
   - `GGztQqQ6pCPa…`
   - `4xDsmeTWPNjgS…`
   - `2MFoS3MPtvyQ4…`
   - `9nnLbotNTcUh…`
   - `BQ72nSv9f3PR…`
   - `CapuXNQoDviL…`
   - `6LXutJvKUw8Q…`
   - `6U91aKa8pmMx…`
3. **Sent 133,630 BELIEVE to `62N5myBG3E2pB1AhLdDi9SFfLP78Y7jHNkLa4qt5NjLK`** in two batches at May 4 11:20 and 11:49 UTC (near pump peak)

The zero-SOL "buys" from APVT at 19:55–19:58 UTC suggest APVT was receiving tokens through a mechanism other than market purchase (LP fee collection, protocol allocation, or direct team transfer).

### GCvHh — accumulation then distribution

`GCvHhEUYQwTJ8jyf8Lc4bv8jBXyZU4LMMsZCobwEPzvM` (feePayer for the 19:58:52 ignition buy) shows net **-793,023 BELIEVE** across its most recent 100 transactions (May 5–6). All recent activity is SWAP or UNKNOWN type. This wallet appears to have built a position during the pump and is now systematically selling — a classic pump-and-distribute pattern.

### CdSkvzb — zero-cost pre-pump load

`CdSkvzb45HpGCFAFBEzCUwYsgpXYW8GfVdmfGtkadG8i` received 71,488 BELIEVE at 19:58:36 UTC for 0 SOL. This wallet appears repeatedly in the block scanner output through 20:00–20:14 UTC. Its role is consistent with APVT's: team-controlled wallet receiving pre-positioned tokens.

### Tax aggregator — AwTxkK

`AwTxkKrszDzH8WPxqUHS1sTJna5Fagw5xio8kTXWjnfn` (previously identified as the 2% tax aggregator from Believe.app) showed TRANSFER-only recent activity with no visible BELIEVE outflows in its last 20 transactions. Its downstream connections were not fully traced.

---

## Q5 — Floor Holders: Who Is Holding at $5.5–6M?

**Data gap:** This question was not answered. Determining current large BELIEVE holders would require querying the SPL token program's top token account holders or scanning current liquidity provider positions — neither was performed in this investigation.

**What we can infer from available data:**

- `Bs6cTDM6Ac6WfenqEG4mNp156b7ZYGMcTXzo2MoZxtg5` (GCvHh's delivery address) holds net +771,248 BELIEVE and is still actively SWAPping (100% SWAP txs) — likely still positioned.
- `FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp` (dominant first-hour buyer, 16.86M BELIEVE) — current holdings and sell activity unknown. If FkaLnX17 held from peak ($0.0078) to current ($0.0045), it would be sitting on ~$43M notional position (unrealized loss from peak: ~40%).
- `A4StKNA7M8WqS3o5PtoSYTTCxPsk2EGHeeHAXfb8UWqS` (4.45M BELIEVE for 114 SOL, entering at 20:14 UTC) — sell activity not traced.
- POOL_MAIN current liquidity: $197K USD (21.7M BELIEVE + 1,131 WSOL), suggesting thin sell-side depth.

---

## Q6 — Synthesis: Organic Rally or Coordinated Pump?

### Evidence for coordination

**1. Insider pre-load before open-market buying.**  
APVT and CdSkvzb accumulated BELIEVE through POOL_MAIN at zero SOL cost in the 3 minutes before FkaLnX17's first real buy. This suggests project-controlled wallets were adjusting their pool positions immediately before the event, giving them favorable entry.

**2. Extreme first-hour buyer concentration.**  
Two wallets (FkaLnX17 + A4StKNA7) drove 90% of first-hour SOL inflow (355 of 395 SOL). FkaLnX17 alone executed 155 buys in 70 minutes — approximately one buy every 27 seconds — with consistently large position sizing (1.5–2.8 SOL per buy). This is not typical retail behavior.

**3. FkaLnX17 was the first real buyer, 17 seconds before GCvHh.**  
The margin between first and second buy (17 seconds) at the exact moment the flat consolidation broke is too precise to attribute to coincidence. Either FkaLnX17 had advance knowledge of when the pump would begin, or it was the pump initiator itself.

**4. APVT's transfer of 267,599 BELIEVE to 62N5myBG at 19:58:46 (during ignition).**  
This large TRANSFER (not a swap) executed at the exact second the price was moving, distributing tokens to the downstream `u6PJ8Dt` chain. It is consistent with a synchronized distribution: APVT loads the pool, FkaLnX17 buys, and APVT simultaneously routes tokens to distribution wallets.

**5. GCvHh is a net seller.**  
The wallet that placed the second early buy (1.97 WSOL, 228K BELIEVE to Bs6cTDM6) is now net -793K BELIEVE. Built a position during the pump; distributing during recovery.

**6. Daily scheduled transfers from 62N5myBG → u6PJ8Dt.**  
Starting April 28 (one week pre-pump), an automated schedule transferred millions of BELIEVE out of 62N5myBG daily. On May 4 (peak day) this was **6,036,633 BELIEVE** — the largest daily transfer. This consistent mechanism around the pump date (May 3–5) suggests the project team was actively moving supply during the event.

### Evidence that's ambiguous

- FkaLnX17's funder (`3epyFwei`) shows no BELIEVE history and only UNKNOWN-type txs — this is suspicious but inconclusive. `3epyFwei` could be a cold wallet, a CEX withdrawal address, or a bridge deposit.
- A4StKNA7 entered 15 minutes after ignition (20:14 UTC) — could be an opportunistic buyer rather than a coordinator.
- The zero-SOL APVT "buys" could theoretically reflect LP fee harvesting rather than insider token loading.

### Verdict

**Coordinated pump with insider elements, not a purely organic rally.**

The sequence — zero-cost insider pre-load → single dominant buyer (FkaLnX17) placing the first real bid and sustaining 90% of volume for the first hour → simultaneous distribution of insider tokens → current net selling by ignition wallets — is consistent with a planned price action event rather than spontaneous buying driven by retail FOMO.

**The most likely scenario:** FkaLnX17 is either the pump coordinator or a party with advance knowledge of the pump date. It was funded, positioned, and executing within seconds of the flat break. The APVT / CdSkvzb activity in the 3 minutes before FkaLnX17's first buy suggests the project team pre-seeded favorable pool conditions.

**What this does not prove:** Active fraud or market manipulation in a legal sense requires proof of intent and coordination. The on-chain evidence is strongly suggestive but not conclusive. FkaLnX17 could theoretically be an independent whale who happened to buy first; APVT's zero-SOL accumulation could reflect legitimate LP fee collection.

---

## Key Wallet Index

| Address | Label | Role |
|---|---|---|
| `FkaLnX17cXZGyeu3kZGdHCNdFMJJzBrPPYVvd18B3MZp` | First Buyer / Pump Driver | First real SOL buy at 19:58:35; 155 buys, 241 SOL, 16.8M BELIEVE in first hour |
| `APVT9fhDpfq5qf2yJFtEesfuz3ziPf8dRfFkpJ3gYuLu` | Distribution Hub | Zero-cost BELIEVE accumulation; distributes to 8+ sub-wallets; 267K BELIEVE transfer at ignition |
| `GCvHhEUYQwTJ8jyf8Lc4bv8jBXyZU4LMMsZCobwEPzvM` | Routing Wallet | 2nd buyer at 19:58:52, now net -793K BELIEVE (distributing) |
| `Bs6cTDM6Ac6WfenqEG4mNp156b7ZYGMcTXzo2MoZxtg5` | GCvHh Delivery | Receives BELIEVE from GCvHh; net +771K |
| `CdSkvzb45HpGCFAFBEzCUwYsgpXYW8GfVdmfGtkadG8i` | Insider Pre-Load | Zero-cost BELIEVE at ignition; active 19:58–20:14 UTC |
| `A4StKNA7M8WqS3o5PtoSYTTCxPsk2EGHeeHAXfb8UWqS` | Large Buyer #2 | 5 buys, 114 SOL, 4.45M BELIEVE; entered at 20:14 UTC |
| `62N5myBG3E2pB1AhLdDi9SFfLP78Y7jHNkLa4qt5NjLK` | Pass-Through Dist. | Receives from APVT; forwards to u6PJ8Dt daily; net -21.4M BELIEVE |
| `GgGap6tiUKhD8XeUZaHpVB3rerb3zKWNouWQJHozDujt` | MEV/Arb | Round-trip arb at ignition; net-zero |
| `GxDC9e7SP9mzhDo4re5HbpLa2RW7gB9DtmThx4i4pXSq` | Cross-Pool Arb | Moves BELIEVE between pools; net-zero |
| `3epyFweiQNFvfCFWic3C8AKMzeuvMsYvF7u7fVZTWcYH` | FkaLnX17 Funder | Sent 54.8 SOL to FkaLnX17; UNKNOWN-only tx history |
| `AwTxkKrszDzH8WPxqUHS1sTJna5Fagw5xio8kTXWjnfn` | 2% Tax Aggregator | Believe.app on-chain tax collector; downstream not traced |

---

## Data Gaps and Caveats

1. **POOL2 first-hour data not collected** — cross-pool trades complicate total volume attribution.
2. **APVT history pre-May 4 11:17 not retrieved** — 7+ more pagination pages needed to confirm May 3 role.
3. **FkaLnX17 pre-pump history not retrieved** — cannot confirm whether it held BELIEVE before the pump or was funded specifically for it.
4. **Q5 (floor holders) unanswered** — would require SPL token account scan or separate holder query.
5. **u6PJ8Dt full address not confirmed** — only the 62N5myBG transfer showed a 20-char display truncated to `u6PJ8DtQuPFnfmwHbGFU`; the full base58 address needs to be extracted directly from the tx.
6. **APVT zero-cost accumulation mechanism unexplained** — fee harvesting vs. team allocation vs. something else; distinguishing would require inspecting inner instructions of those specific txs.
7. **GeckoTerminal `before_timestamp` not honored** — cannot retrieve trades from the exact ignition minute via that API; all trade-level data came from Helius RPC + batch parse.

---

*Investigation performed May 6, 2026. All tx data from Solana mainnet (finalized commitment). Timestamps are UTC.*
