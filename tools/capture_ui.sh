#!/bin/bash
# capture_ui.sh URL OUT [wait-seconds]
set -u
URL="${1:?url}"
OUT="${2:?output}"
WAIT="${3:-9}"
PROFILE="/tmp/lora95-capture-profile"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/alex/.Xauthority}"
rm -rf "$PROFILE"
setsid chromium \
  --kiosk --app="$URL" --window-size=1280,720 --window-position=0,0 \
  --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
  --disable-infobars --disable-session-crashed-bubble --disable-translate \
  --password-store=basic --use-mock-keyring --noerrdialogs \
  --force-device-scale-factor=1 --disable-gpu \
  > /tmp/lora95-capture.log 2>&1 < /dev/null &
PID=$!
sleep "$WAIT"
xfce4-screenshooter -f -s "$OUT" >/tmp/lora95-shot.log 2>&1 || true
if [ ! -s "$OUT" ]; then
  import -window root "$OUT" 2>/dev/null || true
fi
kill -TERM -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
sleep 1
kill -KILL -"$PID" 2>/dev/null || true
ls -l "$OUT"
