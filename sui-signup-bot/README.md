# sui-signup-bot

A small Python bot for **testing** a Sui wallet signup workflow in a real
(headless) browser, against a bundled **local mock signup page**.

For each wallet, one at a time, it:

1. opens Chromium;
2. connects the wallet;
3. signs the page's authentication message;
4. submits the signup form;
5. records the result, then waits a random 4–7 minutes before the next one.

## Safety boundaries (built in, not optional)

- **Local targets only.** Adapters only accept loopback URLs
  (`127.0.0.1`, `localhost`, `::1`). The browser also aborts every request to
  any other host.
- **Keys stay in Python.** The browser gets a Wallet Standard shim, and Python
  signs only when that shim asks.
  - Only `sui:signPersonalMessage` is exposed, and only for the adapter's own
    origin.
  - There is no transaction signing, so nothing can move funds.
- **No bypassing of site protections.**
  - CAPTCHA or human check: the wallet is marked `needs_human` and the run
    stops (exit code 3).
  - Rate limit: the whole queue pauses for 30 minutes.
  - Origin and auth checks are satisfied the normal way, by using the real
    page.
  - There is no proxy or IP rotation, no fingerprint or user-agent spoofing,
    and no parallelism.

These boundaries exist on purpose. Before pointing an adapter at any real
signup, get the site operator's permission for automated and multi-wallet
registration, and keep these controls in place.

## Layout

```
suibot/
  keys.py        Sui Ed25519 keys: generate/import (suiprivkey1… or hex), address,
                 personal-message signing, encryption helpers
  db.py          SQLite: encrypted keys, queue/status, attempt history, pacing gate
  browser.py     Playwright Chromium + injected Wallet Standard wallet + network guard
  runner.py      sequential loop, 4–7 min random spacing, retries, resume
  cli.py         command line
  adapters/
    base.py      SignupAdapter interface + error kinds + CAPTCHA detection
    mock_site.py adapter for the bundled mock page  ← all site-specific selectors live here
mock_site/
  index.html     mock signup page (wallet discovery, connect, sign, form)
  server.py      mock API: challenge/verify/signup, Origin check, real Sui signature
                 verification, optional --rate-limit-per-min, --captcha, --fail-rate
deploy/          systemd units
tests/           unit + real-browser end-to-end tests
```

The key handling was cross-checked against the official `@mysten/sui` SDK
(v2.33.1). Addresses, `suiprivkey` encoding and personal-message signatures
verify in both directions.

## How the flow works

1. Keys are stored Fernet-encrypted in SQLite, with a key derived by scrypt
   from your passphrase. Only one wallet is decrypted at a time, in memory.
2. Each attempt gets a fresh browser with its own storage. An init script
   registers a wallet named **"Suibot Test Wallet"** via the Wallet Standard
   events, before the page loads.
3. The adapter drives the page:
   - clicks **Connect wallet** and picks the test wallet;
   - fills the form and clicks **Sign up**.
4. The page requests a challenge and calls `signPersonalMessage`. The shim
   passes the bytes to Python, which signs them in Sui format. The page then
   verifies, checks for an existing signup and submits the form.
5. The adapter reads the result and returns `registered` or
   `already_registered`, or raises one of these error kinds:

| Error kind | What the runner does |
|---|---|
| `retryable` (timeouts, 5xx) | Retries after ~15 min, then 30 min, up to `--max-attempts` (default 3) |
| `rate_limited` | Pauses the whole queue 30 min, then retries |
| `human_check` | Marks the wallet `needs_human` and stops |
| `permanent` (other 4xx) | Marks the wallet `failed` |

Each attempt is committed to SQLite before the next network step:
- Wallets are `pending`, `in_progress`, `done`, `failed` or `needs_human`.
- Each attempt is stored with a timestamp, success flag, result and error.
- The "next signup not before" time is persisted.

After a restart, `done` wallets are never retried and an unfinished wait
resumes where it left off. A wallet interrupted mid-attempt goes back to
`pending`, and the page's existing-signup check turns it into
`already_registered` rather than a duplicate.

## Local quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium          # add --with-deps on a fresh Linux box

export SUIBOT_PASSPHRASE='choose-a-strong-passphrase'

# Wallets you control: generate new ones...
.venv/bin/python -m suibot wallets generate --count 3
# ...and/or import existing ones: one `suiprivkey1...` or 64-char hex per line, optional ",label"
.venv/bin/python -m suibot wallets import keys.txt && shred -u keys.txt
.venv/bin/python -m suibot wallets list

# Terminal 1: the mock site (open http://127.0.0.1:8080/signup to see it)
.venv/bin/python -m mock_site --port 8080

# Terminal 2
.venv/bin/python -m suibot run --dry-run                 # show the schedule; no browser, no writes
.venv/bin/python -m suibot run --once                    # one wallet, then exit
.venv/bin/python -m suibot run                           # whole queue, 4–7 min apart
.venv/bin/python -m suibot run --min-delay 5 --max-delay 10   # faster, for local testing
.venv/bin/python -m suibot status --attempts
.venv/bin/python -m suibot reset --failed                # re-queue failures
```

Mock-site switches for testing failure handling:
- `--rate-limit-per-min 1`
- `--captcha` (the bot must stop)
- `--fail-rate 0.3`
- `--data signups.json` (keeps signups across server restarts)

To export a generated wallet into a wallet app:

```bash
python -m suibot wallets export 0x…
```

This prints its `suiprivkey`. Handle the output carefully.

Tests:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## Running on a VPS (Ubuntu/Debian, systemd)

```bash
# 1. System packages and a dedicated user
sudo apt update && sudo apt install -y python3 python3-venv git
sudo useradd --system --create-home --home-dir /var/lib/suibot --shell /usr/sbin/nologin suibot
sudo chmod 700 /var/lib/suibot

# 2. Code + virtualenv + Chromium
sudo git clone <your-repo-url> /opt/sui-signup-bot-src
sudo cp -r /opt/sui-signup-bot-src/sui-signup-bot /opt/sui-signup-bot
sudo chown -R suibot:suibot /opt/sui-signup-bot
cd /opt/sui-signup-bot
sudo -u suibot python3 -m venv .venv
sudo -u suibot .venv/bin/pip install -r requirements.txt
sudo .venv/bin/playwright install-deps chromium                       # OS libraries (root)
sudo -u suibot env PLAYWRIGHT_BROWSERS_PATH=/opt/sui-signup-bot/.browsers \
     .venv/bin/playwright install chromium

# 3. Passphrase file (readable only by the service user)
sudo install -d -m 750 -o root -g suibot /etc/suibot
sudo bash -c 'umask 027; read -rsp "Passphrase: " p; echo; printf %s "$p" > /etc/suibot/passphrase'
sudo chgrp suibot /etc/suibot/passphrase && sudo chmod 640 /etc/suibot/passphrase

# 4. Wallets
BOT="sudo -u suibot /opt/sui-signup-bot/.venv/bin/python -m suibot --db /var/lib/suibot/suibot.db --passphrase-file /etc/suibot/passphrase"
$BOT wallets generate --count 5
# or: sudo -u suibot tee /var/lib/suibot/keys.txt >/dev/null  (paste, Ctrl-D), then
#     $BOT wallets import /var/lib/suibot/keys.txt && sudo shred -u /var/lib/suibot/keys.txt

# 5. Dry run, then install the services
$BOT run --dry-run
sudo cp deploy/mock-site.service deploy/suibot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mock-site suibot

# 6. Watch it
journalctl -u suibot -f
$BOT status --attempts
```

Service behaviour:
- **Reboots.** Both units start at boot. The bot resumes from SQLite and
  skips completed wallets.
- **Stopping.** `systemctl stop suibot` sends SIGTERM. The bot finishes its
  current step and exits cleanly.
- **Human check.** On exit code 3 the service does not auto-restart. Look at
  the screenshot in `/var/lib/suibot/screenshots/`, run
  `$BOT reset --needs-human`, then `systemctl start suibot`.
- **Wrong passphrase.** The service exits and systemd retries every 60 s.
  Check `journalctl -u suibot` if the bot isn't making progress.
- **Backups.** Back up `/var/lib/suibot/suibot.db` together with the
  passphrase. Without the passphrase the keys can't be recovered.

## Adding an adapter

1. Subclass `SignupAdapter` in `suibot/adapters/`.
2. Implement `signup(page, address)`, using the page the way a user would.
3. Register the class in `suibot/adapters/__init__.py`.

Keep site-specific selectors and result parsing inside the adapter. Map the
site's responses to the error kinds above, and call
`self.check_for_human_check(page)` wherever a challenge might appear.
