#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HELPER_SOURCE="$HERE/affinity-helper.py"
HELPER_DEST="/usr/local/libexec/primegrid-affinity-helper.py"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script from your normal account with: sudo bash install-affinity-helper.sh" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
  echo "Run this with sudo from the desktop account that uses PrimeGrid Affinity; do not run it from a root login shell." >&2
  exit 1
fi
if [[ ! "$TARGET_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*\$?$ ]] || ! getent passwd "$TARGET_USER" >/dev/null; then
  echo "Could not validate the invoking desktop account." >&2
  exit 1
fi
if [[ ! -r "$HELPER_SOURCE" ]]; then
  echo "Missing affinity-helper.py beside this installer." >&2
  exit 1
fi

VISUDO="$(command -v visudo || true)"
if [[ -z "$VISUDO" ]]; then
  echo "visudo is required to validate the sudoers rule; install your distribution's sudo package first." >&2
  exit 1
fi

SUDOERS_DEST="/etc/sudoers.d/primegrid-affinity-$(id -u "$TARGET_USER")"
TMP_RULE="$(mktemp)"
trap 'rm -f "$TMP_RULE"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/bin/python3 -I %s apply *\n' "$TARGET_USER" "$HELPER_DEST" > "$TMP_RULE"
chown root:root "$TMP_RULE"
chmod 0440 "$TMP_RULE"
"$VISUDO" -cf "$TMP_RULE" >/dev/null

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$HELPER_SOURCE" "$HELPER_DEST"
install -o root -g root -m 0440 "$TMP_RULE" "$SUDOERS_DEST"
"$VISUDO" -cf "$SUDOERS_DEST" >/dev/null

echo "Installed restricted PrimeGrid affinity helper for user: $TARGET_USER"
echo "Helper: $HELPER_DEST"
echo "Sudoers rule: $SUDOERS_DEST"
echo "The helper can change affinity only for live executables in BOINC's PrimeGrid project directory."
echo "It does not change BOINC's service account or data-directory ownership."
