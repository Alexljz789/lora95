#!/bin/sh
# ExecStopPost safety net: never leave meshtasticd stopped after a LoRa95 crash.
set -u
if [ -e /run/lora95/meshtasticd_was_active ]; then
    /usr/bin/systemctl start meshtasticd.service >/dev/null 2>&1 || true
    rm -f /run/lora95/meshtasticd_was_active
fi
exit 0
