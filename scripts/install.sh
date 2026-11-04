#!/bin/bash
# Install LoRa95 into /opt/lora95, its systemd units and the desktop shortcut.
#
# Usage: sudo ./scripts/install.sh
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/opt/lora95"
USER_NAME="${LORA95_USER:-alex}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"

if [ -z "$USER_HOME" ]; then
    echo "User '$USER_NAME' not found. Set LORA95_USER=..." >&2
    exit 1
fi
for f in app/web.py app/daemon.py scripts/lora95-ui scripts/lora95.desktop; do
    [ -e "$SRC/$f" ] || { echo "missing $SRC/$f" >&2; exit 1; }
done

echo "==> Stopping old services"
"$SYSTEMCTL" stop lora95-web.service >/dev/null 2>&1 || true
"$SYSTEMCTL" stop lora95-radio.service >/dev/null 2>&1 || true

echo "==> Installing to $DEST"
rm -rf "$DEST"
install -d -m 0755 "$DEST"
cp -a "$SRC/app" "$DEST/app"
cp -a "$SRC/scripts" "$DEST/scripts"
cp -a "$SRC/icon.png" "$DEST/icon.png"
for doc in README.md README.zh-CN.md LICENSE THIRD_PARTY_NOTICES.md requirements.txt; do
    [ -e "$SRC/$doc" ] && cp -a "$SRC/$doc" "$DEST/$doc"
done
find "$DEST" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$DEST" -type f -name '*.pyc' -delete
chmod 0755 "$DEST/scripts/lora95-ui" "$DEST/scripts/restore-meshtastic.sh"
chmod 0755 "$DEST/lora95-ui" 2>/dev/null || true
if [ -f "$DEST/scripts/lora95-ui" ]; then
    ln -sf "$DEST/scripts/lora95-ui" "$DEST/lora95-ui"
fi
chown -R root:root "$DEST"
find "$DEST" -type d -exec chmod 0755 {} +
find "$DEST" -type f -exec chmod 0644 {} +
chmod 0755 "$DEST/scripts/lora95-ui" "$DEST/scripts/restore-meshtastic.sh"
if [ -L "$DEST/lora95-ui" ]; then
    true
fi

echo "==> Installing systemd units"
install -m 0644 "$SRC/scripts/lora95-radio.service" /etc/systemd/system/lora95-radio.service
install -m 0644 "$SRC/scripts/lora95-web.service" /etc/systemd/system/lora95-web.service
"$SYSTEMCTL" daemon-reload

echo "==> Installing launcher and desktop entry"
ln -sfn "$DEST/scripts/lora95-ui" /usr/local/bin/lora95-ui
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/Desktop"
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.local/share/applications"
install -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$SRC/scripts/lora95.desktop" "$USER_HOME/Desktop/LoRa95.desktop"
install -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$SRC/scripts/lora95.desktop" "$USER_HOME/.local/share/applications/LoRa95.desktop"
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.config/lora95"
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.local/share/lora95"
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.config/lora95-chromium"

# Make XFCE trust the desktop launcher without an "untrusted" prompt.
for desktop in "$USER_HOME/Desktop/LoRa95.desktop" "$USER_HOME/.local/share/applications/LoRa95.desktop"; do
    checksum="$(sha256sum "$desktop" | awk '{print $1}')"
    runuser -u "$USER_NAME" -- env DISPLAY="${DISPLAY:-:0}" XAUTHORITY="$USER_HOME/.Xauthority" \
        gio set "$desktop" metadata::xfce-exe-checksum "$checksum" 2>/dev/null || true
    runuser -u "$USER_NAME" -- env DISPLAY="${DISPLAY:-:0}" XAUTHORITY="$USER_HOME/.Xauthority" \
        gio set "$desktop" metadata::trusted true 2>/dev/null || true
done

echo "==> Installing sudoers rule for the kiosk launcher"
cat >/etc/sudoers.d/lora95 <<EOF
$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL start lora95-radio.service, $SYSTEMCTL stop lora95-radio.service, $SYSTEMCTL start lora95-web.service, $SYSTEMCTL stop lora95-web.service
EOF
chmod 0440 /etc/sudoers.d/lora95
if command -v visudo >/dev/null 2>&1; then
    visudo -cf /etc/sudoers.d/lora95 >/dev/null
fi

echo "==> Checking Python dependencies"
missing=""
for mod in flask cryptography spidev lgpio; do
    if ! /usr/bin/python3 -c "import $mod" >/dev/null 2>&1; then
        missing="$missing $mod"
    fi
done
if [ -n "$missing" ]; then
    echo "WARNING: missing Python modules:$missing" >&2
    echo "Install with: sudo apt install python3-flask python3-cryptography python3-spidev python3-lgpio" >&2
fi

echo
echo "LoRa95 installed."
echo "  Desktop shortcut : $USER_HOME/Desktop/LoRa95.desktop"
echo "  Launcher         : lora95-ui"
echo "  Radio service    : systemctl start lora95-radio.service"
echo "  Web service      : systemctl start lora95-web.service  (http://127.0.0.1:8766)"
echo "  Logs             : journalctl -u lora95-radio -u lora95-web -f"
echo
echo "The services are installed but NOT enabled at boot. The desktop shortcut"
echo "starts them on demand and restores meshtasticd when the window closes."
