#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  echo "Tkinter is not installed. Install it first with:"
  echo "  sudo apt install python3-tk"
  exit 1
fi

python3 -m py_compile "$HERE/primegrid-affinity-gui.py"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$BIN_DIR" "$APP_DIR"
install -m 755 "$HERE/primegrid-affinity-gui.py" "$BIN_DIR/primegrid-affinity-gui"

cat > "$APP_DIR/primegrid-affinity-gui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PrimeGrid Affinity
Comment=Manage several PrimeGrid CPU affinity profiles
Exec=/usr/bin/python3 "$BIN_DIR/primegrid-affinity-gui"
Terminal=false
Categories=Utility;
Icon=applications-system
EOF
chmod 644 "$APP_DIR/primegrid-affinity-gui.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "Installed/updated. Open Zorin's app menu and search for: PrimeGrid Affinity"
echo "No system-wide files were changed; the app is installed in your home folder."
