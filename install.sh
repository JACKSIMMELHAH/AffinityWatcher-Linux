#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Give package-manager-specific dependency instructions without trying to
# install system packages or make privileged changes automatically.
DISTRO_ID=""
DISTRO_LIKE=""
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO_ID="${ID:-}"
  DISTRO_LIKE="${ID_LIKE:-}"
fi
DISTRO_IDS=" ${DISTRO_ID} ${DISTRO_LIKE} "
case "$DISTRO_IDS" in
  *" arch "*|*" manjaro "*|*" endeavouros "*|*" garuda "*)
    PKG_FAMILY="arch"
    ;;
  *" debian "*|*" ubuntu "*|*" linuxmint "*|*" zorin "*|*" pop "*)
    PKG_FAMILY="debian"
    ;;
  *)
    PKG_FAMILY="unknown"
    ;;
esac

print_python_install_hint() {
  case "$PKG_FAMILY" in
    arch)
      echo "  sudo pacman -S python tk"
      ;;
    debian)
      echo "  sudo apt install python3 python3-tk"
      ;;
    *)
      echo "  Install Python 3.8+ and its Tkinter/Tk package using your distribution's package manager."
      ;;
  esac
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it first with:"
  print_python_install_hint
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Python 3.8 or newer is required. Detected: $(python3 --version 2>&1)"
  echo "Install a supported Python 3 version using your distribution's package manager."
  exit 1
fi

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  echo "Tkinter is not installed for $(python3 --version 2>&1). Install it with:"
  print_python_install_hint
  exit 1
fi

python3 -m py_compile "$HERE/primegrid-affinity-gui.py"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
USER_SERVICE_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/primegrid-affinity-monitor.desktop"
MONITOR_SERVICE="primegrid-affinity-monitor.service"
STATE_DIR="$HOME/.local/state/primegrid-affinity-gui"
PID_FILE="$STATE_DIR/monitor.pid"

# Migrate from the earlier systemd-unit build to a normal desktop autostart
# entry. Stop/disable the old unit first, then remove its file so the GUI uses
# its regular PID-managed start/stop path instead of systemd.
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user disable --now "$MONITOR_SERVICE" >/dev/null 2>&1 || true
fi
rm -f "$USER_SERVICE_DIR/$MONITOR_SERVICE"
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

# Stop any pre-service detached monitor before updating the executable so an
# older copy cannot run alongside the newly installed version.
if [[ -r "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$OLD_PID" =~ ^[0-9]+$ && -r "/proc/$OLD_PID/cmdline" ]]; then
    if python3 - "$OLD_PID" <<'PY_STOP_MONITOR'
import os, signal, sys, time
from pathlib import Path
pid = int(sys.argv[1])
try:
    argv = [x.decode(errors="replace") for x in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if x]
    # The PID file is specific to this app. Accept both its installed filename
    # and the source filename so older manually launched monitors are stopped.
    is_our_monitor = "--monitor" in argv and any(
        Path(arg).name in {"primegrid-affinity-gui", "primegrid-affinity-gui.py"}
        for arg in argv
    )
    if not is_our_monitor:
        raise SystemExit(1)
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        print(f"Warning: old monitor PID {pid} did not exit promptly; not force-killing it.", file=sys.stderr)
except (OSError, ValueError, ProcessLookupError):
    pass
PY_STOP_MONITOR
    then
      rm -f "$PID_FILE"
    fi
  fi
fi

mkdir -p "$BIN_DIR" "$APP_DIR" "$AUTOSTART_DIR" "$USER_SERVICE_DIR"
install -m 755 "$HERE/primegrid-affinity-gui.py" "$BIN_DIR/primegrid-affinity-gui"

cat > "$APP_DIR/primegrid-affinity-gui.desktop" <<EOF_DESKTOP
[Desktop Entry]
Type=Application
Name=PrimeGrid Affinity
Comment=Manage several PrimeGrid CPU affinity profiles
Exec=/usr/bin/python3 "$BIN_DIR/primegrid-affinity-gui"
Terminal=false
Categories=Utility;
Icon=applications-system
EOF_DESKTOP
chmod 644 "$APP_DIR/primegrid-affinity-gui.desktop"

# Zorin/GNOME/XDG autostart entry visible in Startup Applications. It launches
# the monitor only; it does not open the GUI window.
cat > "$AUTOSTART_FILE" <<EOF_AUTOSTART
[Desktop Entry]
Version=1.0
Type=Application
Name=PrimeGrid Affinity Monitor
Comment=Apply saved PrimeGrid CPU profiles automatically at login
Exec=/usr/bin/python3 "$BIN_DIR/primegrid-affinity-gui" --monitor
TryExec=/usr/bin/python3
Terminal=false
X-GNOME-Autostart-enabled=true
Categories=Utility;
EOF_AUTOSTART
chmod 755 "$AUTOSTART_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "Installed/updated. Open your desktop's application menu and search for: PrimeGrid Affinity"
echo "Automatic monitor startup entry: $AUTOSTART_FILE"
echo "The monitor starts when you log in, without opening the GUI."
echo "To disable auto-start, rename or remove: $AUTOSTART_FILE"
echo "The monitor uses saved profiles and does not require the GUI to remain open."
echo "No system-wide files were changed by the app installer."
echo "Requirements checked: Python 3.8+ and Tkinter."
echo "If BOINC runs under a separate account and affinity changes are denied, run: sudo bash "$HERE/install-affinity-helper.sh""
