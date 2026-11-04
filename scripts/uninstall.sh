#!/bin/bash
# Remove LoRa95 from the system. User data is kept unless --purge is given.
set -euo pipefail
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi
USER_NAME="${LORA95_USER:-alex}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
"$SYSTEMCTL" stop lora95-web.service >/dev/null 2>&1 || true
"$SYSTEMCTL" stop lora95-radio.service >/dev/null 2>&1 || true
"$SYSTEMCTL" disable lora95-web.service lora95-radio.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/lora95-radio.service /etc/systemd/system/lora95-web.service
"$SYSTEMCTL" daemon-reload
rm -rf /opt/lora95
rm -f /usr/local/bin/lora95-ui
rm -f "$USER_HOME/Desktop/LoRa95.desktop" "$USER_HOME/.local/share/applications/LoRa95.desktop"
rm -f /etc/sudoers.d/lora95
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$USER_HOME/.config/lora95" "$USER_HOME/.local/share/lora95" "$USER_HOME/.config/lora95-chromium"
fi
echo "LoRa95 removed. meshtasticd is left as-is (LoRa95 restores it on normal shutdown)."
