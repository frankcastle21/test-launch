# signup-mock

A **local mock** of the Perpsplexity Phase 1 wallet-signup flow, plus a
restart-safe, paced queue runner. Use it to test VPS infrastructure: scheduling,
wallet handling, persistence, logging and retries. It never submits
registrations to the real service.

The client refuses any base URL that is not loopback (`127.0.0.1`, `::1` or
`localhost`). There is deliberately no flag to change that. See
[Why this is a mock](#why-this-is-a-mock).

## Research summary (as of 2026-09-25)

### Confirmed: rewards and allocations

- There is no published airdrop, token allocation, points system or reward
  schedule tied to the Phase 1 signup. The page, the embedded whitepaper, the
  Terms of use and the Privacy policy (both dated 15 Sep 2026) don't mention
  one. The public Telegram channel preview (`t.me/s/perpsplexity`) holds only
  bot/admin messages.
- The page says "the flywheel is turning", shows a countdown ("List closes
  Sunday 7:00 AM PT", which is `2026-09-27T14:00Z` in the bundle) and a live
  signup count (347 at the time of checking).
- After signup the page shows a referral link. It also shows a "boost"
  multiplier ("Nx boost" / "no boost") for the referrer and for your own code,
  plus "Share for a boost?". Nothing says what a boost applies to.
- A `$PPX` nav entry exists. The whitepaper describes platform mechanics
  (pool fees, USDC dividends, locks), not a signup reward.

### Speculation, not confirmed

- A "list" with referral boosts often precedes a weighted allocation or
  whitelist. Nothing published says this one does, and there are no stated
  amounts, criteria or snapshot rules.
- The X account (`@perpsplexity`) could not be checked from this environment
  (HTTP 402).

### Confirmed: rules on automation and multiple wallets

- The Terms of use don't address the signup, automation, bots, or multiple
  wallets per person.
- They include a general eligibility clause (legal age, lawful jurisdiction)
  and "Using the site after a change means you accept it".
- No rate limits, per-IP/per-person caps or Sybil policy are published.
- `robots.txt` is `Allow: /`, which governs crawling, not account creation.
- Server-side protections, if any, are not visible from the client.
- **Conclusion: the rules neither permit nor forbid automated multi-wallet
  signup. They are unclear, hence this mock.**

## How the real signup works (from the public frontend bundle)

1. **Wallet connect.** Uses Sui dApp Kit with standard Sui wallets (e.g.
   Slush) or Google via zkLogin. Mainnet and testnet are supported.
2. **Challenge.** `POST /api/auth/challenge {address}` returns `{message}`.
3. **Signature.** The wallet signs the message with `sui:signPersonalMessage`
   (the UI says "One signature to confirm it is you. No fees."). No
   transaction and no gas.
4. **Verify.** `POST /api/auth/verify {address, message, signature, network}`
   returns `{token, expiresAt, created, profile}`. The bearer token is cached
   in `localStorage` under `ppx-session:<address>` and reused while it has
   more than 60 s left.
5. **Existing-signup check.** `GET /api/signup` with
   `Authorization: Bearer <token>` returns `{signup | null}`. If a signup
   exists, the UI shows "You're all signed up" and never POSTs.
6. **Signup.** `POST /api/signup {referral}` (Bearer) returns
   `{signup, total}`. The signup has `referredBy`, `boost`, `invited` and
   `codeBoost`.
7. **Referral.** `?referral=<name>` (`^[a-z0-9_]{3,20}$`) is stored in
   `localStorage`. It is resolved via `GET /api/signup/referrer?name=`.
8. **Username.** Your username is your referral code. It is checked with
   `GET /api/profile/username?username=` and saved with `POST /api/profile`.
9. **Live count.** `GET /api/signup/total`, plus SSE on
   `/api/events?network=mainnet` (`{type:"signups", total}`).

Other details:

- **Duplicate wallets.** The client avoids duplicates via the GET pre-check.
  Server-side enforcement isn't observable from the client, but it is almost
  certainly keyed on the authenticated address.
- **Anti-bot measures.** No CAPTCHA or bot-detection script appears in the
  signup bundle. Anything server-side (rate limiting, IP heuristics, Sybil
  analysis) is unknown.

## What the mock reproduces

| Real endpoint | Mock behaviour |
|---|---|
| `POST /api/auth/challenge` | Nonce message, single use, 5-minute TTL |
| `POST /api/auth/verify` | Ed25519 signature check (Sui-style `flag‖sig‖pubkey`, Sui-style address derivation), 24 h token |
| `GET /api/signup` | Returns the existing signup or `null` |
| `POST /api/signup` | Creates a signup; **409 on duplicate** (an assumption) |
| `GET /api/signup/total` | Count |
| — | Fault injection: `--fail-rate`, `--throttle-rate`, `--retry-after`, `--rate-limit-per-min`, `--latency` |

Mock signatures sign the raw message bytes. They don't use Sui's intent-wrapped
personal-message digest, so mock wallets and signatures won't work anywhere
real, and vice versa.

## Runner guarantees

- **One wallet at a time.** Attempts never overlap.
- **Randomized spacing.** Attempts are 4–7 minutes apart by default
  (`--min-delay 240 --max-delay 420`). The pacing gate is persisted, so a
  reboot mid-wait resumes the wait instead of firing immediately.
- **Restart safety.** Every state change is committed to SQLite (WAL,
  `synchronous=FULL`) before the next request.
  - Completed wallets are never retried.
  - A wallet interrupted mid-attempt goes back to pending.
  - The GET-before-POST check turns a lost response into
    `already_registered`, not a duplicate.
- **Recording.** Each attempt goes to an `attempts` table (HTTP status, error,
  server response) and to an append-only `events.jsonl`. Tokens, signatures
  and keys are redacted and never logged.
- **Conservative retries.**
  - 5xx and timeouts retry up to `--max-attempts 4`, with exponential backoff
    from 10 min up to 1 h, ±20 % jitter.
  - Any 429 pauses the whole queue for at least 30 min, or `Retry-After` if
    longer.
  - Other 4xx errors fail the wallet immediately.
  - After 3 consecutive failures the run stops with exit code 2. systemd is
    configured not to auto-restart on that code.
- **Dry run.** `--dry-run` prints the planned schedule. It sends no requests
  and writes no state.
- **No evasion.** There is no proxy or IP rotation, no user-agent or
  fingerprint spoofing, no CAPTCHA handling, and no parallelism.

## Usage

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Throwaway test wallets, encrypted at rest (scrypt + Fernet)
export SIGNUPMOCK_PASSPHRASE='...'        # or --passphrase-file, or interactive prompt
.venv/bin/python -m signupmock gen-wallets --count 10 --out wallets.enc --encrypt
# Or encrypt an existing plaintext list: signupmock encrypt --in wallets.json --out wallets.enc

.venv/bin/python -m signupmock serve --port 8787 --fail-rate 0.1 --throttle-rate 0.02 &

.venv/bin/python -m signupmock run --wallets wallets.enc --dry-run
.venv/bin/python -m signupmock run --wallets wallets.enc --state state.db --log events.jsonl
.venv/bin/python -m signupmock run --wallets wallets.enc --time-scale 0.01   # fast local test
.venv/bin/python -m signupmock status --state state.db
.venv/bin/python -m signupmock retry-failed --state state.db
```

Wallet file (plaintext form, before encryption):

```json
{"format": "signupmock-wallets-v1",
 "wallets": [{"label": "wallet-001", "address": "0x…", "secret_key": "<32-byte hex seed>"}]}
```

On load, each address is re-derived from its key and checked. Duplicate
addresses are rejected. Plaintext files readable by group/other trigger a
warning.

### VPS (systemd)

The units are in `deploy/`: `signupmock-server.service` binds loopback only,
and `signupmock-runner.service` survives reboots but stops on the circuit
breaker. Put the code in `/opt/signup-mock`, the state in `/var/lib/signupmock`
and the passphrase in `/etc/signupmock/passphrase` (mode 0600). Then run:

```bash
systemctl enable --now signupmock-server signupmock-runner
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

The tests cover:

- Spacing bounds and randomness
- Restarts: no repeated wallets, and the persisted wait is honoured
- Crash recovery without a duplicate POST
- Backoff, permanent errors, the 429 global pause and the circuit breaker
- Dry-run making no requests and no writes
- Encryption round-trip and wrong-passphrase handling
- The loopback guard

## Why this is a mock

Nothing published authorizes automated or multi-wallet registration, and
nothing forbids it either. The only incentive visible on the page is a
referral boost. On lists like this, one person registering many wallets is
what projects typically treat as Sybil activity. Some disqualify it after the
fact, even when the terms are silent.

Before pointing anything at the real service, get explicit written permission
from the operator (`support@perpsplexity.app`). Ask them:

1. Are multiple wallets per person allowed?
2. Is automated signup allowed?
3. Is there a rate limit?

Even with that permission, this tool must not be used to get around any rate
limit, CAPTCHA, IP or fingerprint check, or Sybil control.
