#!/bin/bash
# Valerie launcher: first-run setup if needed, then the app. One copy at a time.
APP=/usr/lib/valerie-assistant
DATA="$HOME/.local/share/valerie-assistant"
CONF="$HOME/.config/valerie-assistant/config.json"
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/valerie-assistant-$USER.lock"
if ! flock -n 9; then
    command -v notify-send >/dev/null && notify-send "Valerie" "Valerie is already open."
    exit 0
fi
if [ ! -x "$DATA/venv/bin/python" ] || ! grep -q '"setup_done": true' "$CONF" 2>/dev/null; then
    /usr/bin/python3 "$APP/setup_wizard.py" || exit 0
fi
cd "$APP/app" && exec "$DATA/venv/bin/python" gui_bot.py "$@"
