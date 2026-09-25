#!/usr/bin/env bash
# Remove suibot. Keeps wallets, config and logs unless --purge is given.
#   sudo ./deploy/uninstall.sh
#   sudo ./deploy/uninstall.sh --purge   # also deletes /etc/suibot, /var/lib/suibot (WALLET KEYS),
#                                        # /var/log/suibot and the suibot user
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

if [[ -d /run/systemd/system ]]; then
    systemctl disable --now suibot.service suibot-backup.timer suibot-mock.service 2>/dev/null || true
fi
rm -f /etc/systemd/system/suibot.service /etc/systemd/system/suibot-mock.service \
      /etc/systemd/system/suibot-backup.service /etc/systemd/system/suibot-backup.timer \
      /etc/logrotate.d/suibot /usr/local/bin/suibot
[[ -d /run/systemd/system ]] && systemctl daemon-reload
rm -rf /opt/suibot
echo "Removed code, units, wrapper and logrotate config."

if [[ $PURGE -eq 1 ]]; then
    read -r -p "Type DELETE to erase /var/lib/suibot (encrypted wallet keys), /etc/suibot and logs: " ok
    if [[ "$ok" == DELETE ]]; then
        rm -rf /var/lib/suibot /etc/suibot /var/log/suibot
        userdel suibot 2>/dev/null || true
        echo "Purged data, config, logs and user."
    else
        echo "Purge cancelled; data kept."
    fi
else
    echo "Kept /etc/suibot, /var/lib/suibot and /var/log/suibot."
fi
