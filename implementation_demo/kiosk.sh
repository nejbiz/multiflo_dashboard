#!/bin/sh
set -eu

dashboard_url="http://127.0.0.1:8000/"
kiosk_profile="/home/multiflo-display/.config/multiflo-kiosk"

mkdir -p "$kiosk_profile"

until curl --fail --silent --show-error --max-time 2 "$dashboard_url" >/dev/null; do
    sleep 1
done

exec systemd-inhibit \
    --what=idle \
    --who="MultiFlo dashboard" \
    --why="Touchscreen kiosk is active" \
    --mode=block \
    chromium \
    --kiosk \
    --app="$dashboard_url" \
    --user-data-dir="$kiosk_profile" \
    --password-store=basic \
    --no-first-run \
    --noerrdialogs \
    --disable-session-crashed-bubble \
    --disable-pinch \
    --overscroll-history-navigation=0
