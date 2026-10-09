#!/bin/bash
# Build valerie-assistant_<version>_all.deb from the working app + packaging files.
set -euo pipefail
VERSION="${1:-3.0.0}"
B="$(cd "$(dirname "$0")" && pwd)"
SRC="$B/../app"
P="$B/pkg"
rm -rf "$P"
L="$P/usr/lib/valerie-assistant"
mkdir -p "$P/DEBIAN" "$L/app" "$P/usr/bin" "$P/usr/share/applications" \
         "$P/usr/share/icons/hicolor/scalable/apps" "$P/usr/share/doc/valerie-assistant"

# --- app code (no caches, no snapshots, no user data)
for f in "$SRC"/*.py; do install -m 644 "$f" "$L/app/"; done
cp -r "$B/../defaults" "$L/defaults"
install -m 755 "$B/setup_wizard.py" "$L/setup_wizard.py"
install -m 644 "$B/requirements.txt" "$L/requirements.txt"
install -m 755 "$B/valerie-assistant.sh" "$P/usr/bin/valerie-assistant"
install -m 644 "$B/valerie-assistant.desktop" "$P/usr/share/applications/"
install -m 644 "$B/valerie-assistant.svg" "$P/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$B/README.md" "$P/usr/share/doc/valerie-assistant/README.md"
cat > "$P/usr/share/doc/valerie-assistant/copyright" <<'C'
Valerie Assistant
Copyright (c) 2026 The Valerie Assistant authors. All rights reserved.

Downloaded separately at first run (not included in this package):
  Piper text-to-speech (MIT) - https://github.com/rhasspy/piper
  Piper voice en_US-amy-medium - https://huggingface.co/rhasspy/piper-voices
  Ollama (MIT) and the qwen2.5 model (Apache 2.0) - https://ollama.com
  Python packages from PyPI under their own licenses.
C

# --- privacy gate: refuse to ship anything personal
# Your personal words (names, emails, drive IDs...) live OUTSIDE the repo,
# one regex per line, in ~/.config/valerie-assistant/privacy-patterns.txt
PF="${VALERIE_PRIVACY_PATTERNS:-$HOME/.config/valerie-assistant/privacy-patterns.txt}"
PATTERN='@gmail\.com|BEGIN [A-Z ]*PRIVATE KEY'
[ -f "$PF" ] && PATTERN="$PATTERN|$(grep -v '^[[:space:]]*#' "$PF" | grep -v '^[[:space:]]*$' | paste -sd'|')"
if grep -rniE "$PATTERN" "$P" --include=* ; then
    echo "BUILD STOPPED: personal data found in the package (see above)." >&2
    exit 1
fi

SIZE=$(du -sk "$P/usr" | cut -f1)
cat > "$P/DEBIAN/control" <<CTL
Package: valerie-assistant
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.8), python3-venv, python3-tk, python3-dateutil, python3-requests, python3-psutil, python3-pil, python3-pil.imagetk, curl, alsa-utils, libportaudio2, espeak-ng
Recommends: libnotify-bin, thunderbird, rclone
Conflicts: valerie-local-assistant
Replaces: valerie-local-assistant
Maintainer: Valerie Assistant <valerie-assistant@users.noreply.local>
Description: Valerie - a personal AI assistant that runs on your computer
 Chat with a local AI (Ollama), keep separate workspaces (each with its own
 mailbox, memory, knowledge and time zone), handle email via your mail server
 or Thunderbird, set reminders and scheduled check-ins, and talk hands-free.
 First launch runs a setup wizard that downloads components into your home
 folder; nothing is downloaded or run as root.
CTL
cat > "$P/DEBIAN/postinst" <<'PI'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
echo "Valerie installed. Open 'Valerie Assistant' from your menu to finish setup."
exit 0
PI
cat > "$P/DEBIAN/postrm" <<'PR'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
# User data in ~/.config/valerie-assistant and ~/.local/share/valerie-assistant is left in place.
exit 0
PR
chmod 755 "$P/DEBIAN/postinst" "$P/DEBIAN/postrm"
find "$P" -name __pycache__ -prune -exec rm -rf {} +
OUT="$B/valerie-assistant_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$P" "$OUT" >/dev/null
echo "$OUT"
