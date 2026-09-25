# Deploying suibot on Ubuntu 24.04

This runs the bot and its **local mock signup site** as systemd services on one
VPS. The signup target is the mock on `127.0.0.1`. The bot refuses any
non-loopback URL, and its systemd unit blocks all non-localhost network
traffic at the kernel level.

## 1. Install (copy/paste)

On a fresh Ubuntu 24.04 VPS, as a user with sudo:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone -b claude/eager-thompson-wtnch4 https://github.com/frankcastle21/test-launch.git
cd test-launch/sui-signup-bot
sudo ./deploy/install.sh
```

To use your own passphrase instead of a generated one (at least 12
characters):

```bash
read -rsp 'Keystore passphrase: ' P; echo; printf '%s\n' "$P" | sudo ./deploy/install.sh --passphrase-stdin; unset P
```

Then **back up the passphrase**. Without it, the wallet keys cannot be
decrypted:

```bash
sudo cat /etc/suibot/passphrase
```

The installer:
- installs Python, SQLite and logrotate;
- creates the `suibot` system user (no login shell);
- installs the code to `/opt/suibot`, with a virtualenv and headless Chromium
  (including its system libraries);
- writes the config and passphrase;
- installs the systemd units, logrotate config and the `suibot` admin
  command;
- starts the mock site and the daily backup timer.

It does **not** start the bot until you add wallets.

## 2. Add wallets and start

```bash
# Generate new wallets...
sudo suibot wallets generate --count 5

# ...or import keys you control: one `suiprivkey1…` or 64-char hex per line, optional ",label".
sudo suibot wallets import - < keys.txt && shred -u keys.txt

sudo suibot wallets list
sudo suibot preflight --browser        # DB, passphrase, mock site, Chromium
sudo suibot run --dry-run              # planned schedule; opens nothing, writes nothing
sudo systemctl start suibot
```

## 3. Operate

| Task | Command |
|---|---|
| Follow live progress | `journalctl -u suibot -f` (or `sudo tail -f /var/log/suibot/suibot.log`) |
| Per-wallet status and history | `sudo suibot status --attempts` |
| Service health | `systemctl status suibot suibot-mock suibot-backup.timer` |
| Stop cleanly | `sudo systemctl stop suibot` (finishes the current step; progress is saved) |
| Resume | `sudo systemctl start suibot` |
| Retry failed wallets | `sudo suibot reset --failed && sudo systemctl start suibot` |
| Add wallets later | `sudo suibot wallets generate --count N && sudo systemctl start suibot` |
| Manual backup | `sudo suibot backup` |
| Look at the mock page | `ssh -L 8080:127.0.0.1:8080 you@vps`, then open http://127.0.0.1:8080/signup |

When every wallet is done, the bot exits with code 0 and stays stopped until
you start it again.

## 4. Configuration

Everything is in `/etc/suibot/suibot.env`. After editing it, run
`sudo systemctl restart suibot-mock suibot`.

| Setting | Default | Meaning |
|---|---|---|
| `SUIBOT_MIN_DELAY` / `SUIBOT_MAX_DELAY` | `240` / `420` | Random gap between signups, in seconds (4–7 min) |
| `SUIBOT_MAX_ATTEMPTS` | `3` | Attempts per wallet before it is marked `failed` |
| `SUIBOT_URL` | `http://127.0.0.1:8080` | Mock site URL (loopback only) |
| `SUIBOT_REFERRAL` | empty | Optional referral code typed into the form |
| `SUIBOT_BACKUP_KEEP` | `14` | Daily backups to keep |
| `SUIBOT_VERBOSE` | `0` | `1` for debug logs |
| `MOCK_PORT` | `8080` | Mock site port (keep it in sync with `SUIBOT_URL`) |
| `MOCK_RATE_LIMIT_PER_MIN` / `MOCK_CAPTCHA` / `MOCK_FAIL_RATE` | `0` | Failure injection for testing the bot's handling |

## 5. What is where

| Path | Contents | Owner / mode |
|---|---|---|
| `/opt/suibot` | Code, virtualenv, Chromium | `root:root`, read-only to the service |
| `/etc/suibot/suibot.env` | Configuration | `root:suibot 0640` |
| `/etc/suibot/passphrase` | Keystore passphrase | `root:suibot 0640` |
| `/var/lib/suibot/suibot.db` | SQLite: encrypted keys, queue, attempt history | `suibot 0600`, dir `0700` |
| `/var/lib/suibot/backups/` | Daily consistent DB copies (keys stay encrypted) | `suibot 0700` |
| `/var/lib/suibot/screenshots/` | Browser screenshot on each failed attempt | `suibot 0700` |
| `/var/lib/suibot/mock-signups.json` | Mock site's signups | `suibot 0600` |
| `/var/log/suibot/*.log` | Bot and mock logs | `suibot 0640`, dir `0750` |
| `/etc/systemd/system/suibot*.{service,timer}` | Units | `root 0644` |
| `/etc/logrotate.d/suibot` | Daily rotation, 14 kept, compressed, 50 MB cap | `root 0644` |
| `/usr/local/bin/suibot` | Admin wrapper (runs the CLI as `suibot`) | `root 0755` |

## 6. How reliability is handled

- **Storage.** SQLite runs in WAL mode with `synchronous=FULL`. Each state
  change is committed before the next network step.
- **Encryption.** Private keys are encrypted with Fernet, using a key derived
  from the passphrase with scrypt. A wrong passphrase is detected before
  anything runs.
- **Startup.** `ExecStartPre` runs `suibot preflight`, which:
  - checks database integrity;
  - verifies the passphrase;
  - waits up to 90 s for the mock site.

  The runner then re-queues any wallet left `in_progress` by a crash, kill or
  power loss, and logs it as `interrupted`. The site's "already signed up"
  check means a re-queued wallet is never submitted twice.
- **Pacing survives restarts.** The "next signup not before" time is stored
  in the database, so a restart never skips the 4–7 minute gap.
- **Shutdown.** `KillMode=mixed` sends SIGTERM to the bot only. The bot
  finishes its current step, commits and exits, and systemd then cleans up
  Chromium. The stop timeout is 120 s.
- **Automatic restart.**
  - Crashes restart after 60 s.
  - The retry limit is 5 starts in 15 minutes, which prevents a restart loop
    on a wrong passphrase.
  - Exit code 3 (a CAPTCHA or human check) never auto-restarts; see below.
  - The mock site always restarts.
- **Reboots.** All units are enabled. After a reboot the mock starts, then the
  bot resumes where it left off.
- **Logs.** The bot and mock use `WatchedFileHandler`, so they reopen their
  files after rotation without a restart. Everything also goes to the journal.

### If it stops for a human check

The bot never solves CAPTCHAs.

1. Look at the latest screenshot in `/var/lib/suibot/screenshots/`.
2. Fix the cause (on the mock, set `MOCK_CAPTCHA=0` and restart
   `suibot-mock`).
3. Run:

```bash
sudo suibot reset --needs-human
sudo systemctl start suibot
```

## 7. Security notes

- The service runs as the unprivileged `suibot` user:
  - no capabilities, `NoNewPrivileges`;
  - read-only system (`ProtectSystem=strict`), no `/home` access, private
    `/tmp`;
  - write access only to `/var/lib/suibot` and `/var/log/suibot`.
- `IPAddressDeny=any` with `IPAddressAllow=localhost`: the bot's processes,
  Chromium included, cannot reach the internet.
- The key and passphrase never enter the browser. Chromium gets a Wallet
  Standard shim that asks Python to sign, and only personal messages from the
  mock's own origin are signed. There is no transaction signing.
- The mock site binds to `127.0.0.1` only; it is not reachable from outside
  the VPS.
- Anyone with root, or who can read both the passphrase and the database, can
  decrypt the keys. Keep off-server backups of the database and the
  passphrase **separately**.
- The passphrase can't be changed in place. To rotate it:
  1. Export the keys (`sudo suibot wallets export <addr>`).
  2. Reinstall with a new passphrase and a fresh database.
  3. Re-import the keys.

## 8. Upgrade and uninstall

```bash
cd test-launch && git pull && cd sui-signup-bot
sudo ./deploy/install.sh      # keeps config, passphrase and data; restarts the bot if it was running

sudo ./deploy/uninstall.sh            # removes code/units; keeps config, data and logs
sudo ./deploy/uninstall.sh --purge    # also deletes wallets, config, logs and the user (asks first)
```

To restore a backup:

```bash
sudo systemctl stop suibot
sudo install -m 600 -o suibot -g suibot /var/lib/suibot/backups/<file>.db /var/lib/suibot/suibot.db
sudo rm -f /var/lib/suibot/suibot.db-wal /var/lib/suibot/suibot.db-shm
sudo systemctl start suibot
```
