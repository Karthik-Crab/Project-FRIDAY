#!/usr/bin/env bash
# Opens FRIDAY's chat app in the browser (Linux).
#
#   ./start-friday.sh                  start FRIDAY (or just open the page if it is running)
#   ./start-friday.sh --make-shortcut  add a FRIDAY icon to the desktop and the app menu
#
# Keep the window that opens; closing it stops FRIDAY.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
port="${FRIDAY_PORT:-8765}"
url="http://127.0.0.1:${port}"
friday="${here}/backend/.venv/bin/friday"

if [ "${1:-}" = "--make-shortcut" ]; then
    entry="[Desktop Entry]
Type=Application
Name=FRIDAY
Comment=Chat, pictures, music and voice with FRIDAY
Exec=\"${here}/start-friday.sh\"
Icon=internet-chat
Terminal=true
Categories=Utility;"
    apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    desktop="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    mkdir -p "$apps" "$desktop"
    for file in "$apps/friday.desktop" "$desktop/FRIDAY.desktop"; do
        printf '%s\n' "$entry" > "$file"
        chmod +x "$file"
    done
    echo "Added FRIDAY to the desktop and the app menu."
    exit 0
fi

stop() {
    echo
    echo "$1"
    read -r -p "Press Enter to close this window. " _
    exit 1
}

if curl -s -o /dev/null -m 2 "${url}/api/status"; then
    echo "FRIDAY is already running. Opening it in the browser."
    xdg-open "$url" >/dev/null 2>&1
    sleep 2
    exit 0
fi

[ -x "$friday" ] || stop "FRIDAY is not installed in ${here}/backend/.venv yet."

echo "Starting FRIDAY. The browser opens by itself when it is ready."
echo "Keep this window open while you use FRIDAY. Close it to stop FRIDAY."
echo
cd "${here}/backend" || exit 1
"$friday" app || stop "FRIDAY stopped with a problem (see the lines above)."
