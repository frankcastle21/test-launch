#!/usr/bin/env bash
# Install or upgrade suibot on Ubuntu 24.04. Idempotent: re-run it to upgrade.
#
#   sudo ./deploy/install.sh                     # generate a random keystore passphrase
#   sudo ./deploy/install.sh --passphrase-stdin  # read your own passphrase from stdin
#
# Layout:
#   /opt/suibot            code, virtualenv, Chromium       root:root   read-only to service
#   /etc/suibot            suibot.env, passphrase           root:suibot 0750 / files 0640
#   /var/lib/suibot        SQLite DB, backups, screenshots  suibot      0700
#   /var/log/suibot        log files (logrotate)            suibot      0750
set -euo pipefail

APP=/opt/suibot
CONF=/etc/suibot
DATA=/var/lib/suibot
LOGS=/var/log/suibot
SVC_USER=suibot
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS_STDIN=0

for arg in "$@"; do
    case "$arg" in
        --passphrase-stdin) PASS_STDIN=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
[[ -f "$SRC/suibot/cli.py" && -f "$SRC/mock_site/server.py" ]] || die "run from the sui-signup-bot checkout"
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || echo "WARNING: tested on Ubuntu 24.04; this is ${PRETTY_NAME:-unknown}"

# Read the passphrase before apt/pip/playwright run, so nothing else consumes stdin.
STDIN_PASSPHRASE=""
if [[ $PASS_STDIN -eq 1 ]]; then
    IFS= read -r STDIN_PASSPHRASE || true
    [[ ${#STDIN_PASSPHRASE} -ge 12 ]] || die "passphrase must be at least 12 characters"
fi
exec </dev/null

HAVE_SYSTEMD=0
[[ -d /run/systemd/system ]] && HAVE_SYSTEMD=1

WAS_RUNNING=0
if [[ $HAVE_SYSTEMD -eq 1 ]] && systemctl is-active --quiet suibot.service; then
    WAS_RUNNING=1
    say "Stopping suibot for upgrade (finishes the current step first)"
    systemctl stop suibot.service
fi

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 logrotate ca-certificates

say "Creating service user '$SVC_USER'"
if ! id "$SVC_USER" &>/dev/null; then
    useradd --system --user-group --home-dir "$DATA" --no-create-home \
            --shell /usr/sbin/nologin "$SVC_USER"
fi

say "Creating directories"
install -d -m 0755 -o root -g root "$APP"
install -d -m 0750 -o root -g "$SVC_USER" "$CONF"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$DATA" "$DATA/backups" "$DATA/screenshots"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$LOGS"

say "Installing code to $APP"
rm -rf "$APP/suibot" "$APP/mock_site"
cp -r "$SRC/suibot" "$SRC/mock_site" "$APP/"
install -m 0644 "$SRC/requirements.txt" "$APP/requirements.txt"
[[ -f "$SRC/DEPLOY.md" ]] && install -m 0644 "$SRC/DEPLOY.md" "$APP/DEPLOY.md"
[[ -f "$SRC/README.md" ]] && install -m 0644 "$SRC/README.md" "$APP/README.md"
find "$APP/suibot" "$APP/mock_site" -name __pycache__ -prune -exec rm -rf {} +

say "Creating virtualenv and installing Python dependencies"
[[ -x "$APP/venv/bin/python" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

say "Installing headless Chromium and its system libraries"
PLAYWRIGHT_BROWSERS_PATH="$APP/browsers" "$APP/venv/bin/playwright" install --with-deps --only-shell chromium

"$APP/venv/bin/python" -m compileall -q "$APP/suibot" "$APP/mock_site"
chown -R root:root "$APP"
chmod -R u=rwX,go=rX "$APP"

say "Installing configuration"
if [[ ! -f "$CONF/suibot.env" ]]; then
    install -m 0640 -o root -g "$SVC_USER" "$SRC/deploy/suibot.env" "$CONF/suibot.env"
else
    echo "Keeping existing $CONF/suibot.env (new template: $SRC/deploy/suibot.env)"
    chown root:"$SVC_USER" "$CONF/suibot.env"; chmod 0640 "$CONF/suibot.env"
fi

NEW_PASSPHRASE=0
if [[ ! -s "$CONF/passphrase" ]]; then
    if [[ $PASS_STDIN -eq 1 ]]; then
        PASSPHRASE="$STDIN_PASSPHRASE"
    else
        PASSPHRASE="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
        NEW_PASSPHRASE=1
    fi
    ( umask 077; printf '%s' "$PASSPHRASE" > "$CONF/passphrase" )
    unset PASSPHRASE STDIN_PASSPHRASE
else
    echo "Keeping existing $CONF/passphrase"
fi
chown root:"$SVC_USER" "$CONF/passphrase"
chmod 0640 "$CONF/passphrase"

say "Installing admin wrapper, systemd units and logrotate config"
install -m 0755 -o root -g root "$SRC/deploy/bin/suibot" /usr/local/bin/suibot
for unit in suibot.service suibot-mock.service suibot-backup.service suibot-backup.timer; do
    install -m 0644 -o root -g root "$SRC/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
install -m 0644 -o root -g root "$SRC/deploy/logrotate-suibot" /etc/logrotate.d/suibot
# Pre-create log files with the right owner so root-run tools never create them root-owned.
for f in suibot.log mock-site.log; do
    [[ -e "$LOGS/$f" ]] || install -m 0640 -o "$SVC_USER" -g "$SVC_USER" /dev/null "$LOGS/$f"
done

if [[ $HAVE_SYSTEMD -eq 1 ]]; then
    say "Enabling services"
    systemctl daemon-reload
    systemctl enable --now suibot-mock.service suibot-backup.timer
    systemctl restart suibot-mock.service
    systemctl enable suibot.service
    if [[ $WAS_RUNNING -eq 1 ]]; then
        systemctl start suibot.service
        echo "suibot restarted after upgrade."
    fi
else
    echo "WARNING: systemd is not running here; units were installed but not enabled."
fi

say "Done"
if [[ $NEW_PASSPHRASE -eq 1 ]]; then
    cat <<EOF
A random keystore passphrase was generated in $CONF/passphrase.
BACK IT UP NOW (e.g. in your password manager). Without it the wallet keys
in $DATA/suibot.db cannot be decrypted:
    sudo cat $CONF/passphrase
EOF
fi
cat <<EOF

Next steps:
  sudo suibot wallets generate --count 5        # or: sudo suibot wallets import - < keys.txt
  sudo suibot run --dry-run                     # show the schedule, touch nothing
  sudo systemctl start suibot                   # start processing
  journalctl -u suibot -f                       # follow progress
  sudo suibot status --attempts
EOF
