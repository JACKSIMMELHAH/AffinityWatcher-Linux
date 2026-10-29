PrimeGrid Affinity v5
====================

INSTALLATION
------------
Requirements: Python 3.8+ and Tkinter.

Ubuntu / Debian / Zorin:
    sudo apt install python3 python3-tk

1. Extract the ZIP.
2. Open a terminal in the extracted folder and run:
       bash install.sh
3. Open your application menu and launch "PrimeGrid Affinity".

BOINC PERMISSION WORKAROUND
---------------------------
Use this only if applying affinity fails with "Operation not permitted" and
BOINC runs tasks under the separate `boinc` account. Check the service first:

    systemctl status boinc-client --no-pager
    systemctl show boinc-client -p User -p Group -p WorkingDirectory

This workaround runs BOINC as your normal desktop user, reducing isolation:
project applications may be able to access files available to your user. A
restricted privileged helper would be safer, but is not included here. Stop
active work or let it checkpoint before proceeding.

For a standard Ubuntu / Debian / Zorin APT installation, the data directory is
usually `/var/lib/boinc-client`. Confirm that the service and directory match
your installation before continuing.

Run these commands as your normal desktop user, not from a root shell:

    set -e
    DATA_DIR=/var/lib/boinc-client
    test -d "$DATA_DIR" || { echo "BOINC data directory not found; stop and check your installation."; exit 1; }
    u="$(id -un)"
    g="$(id -gn)"
    sudo systemctl stop boinc-client
    sudo install -d /etc/systemd/system/boinc-client.service.d
    printf '[Service]\nUser=%s\nGroup=%s\nSupplementaryGroups=boinc\n' "$u" "$g" | \
      sudo tee /etc/systemd/system/boinc-client.service.d/affinity-user.conf >/dev/null
    sudo chown -R "$u:$g" "$DATA_DIR"
    sudo systemctl daemon-reload
    sudo systemctl start boinc-client

UNDO THE WORKAROUND
-------------------
For a standard APT installation where the original service user and group are
both `boinc`, run:

    set -e
    DATA_DIR=/var/lib/boinc-client
    sudo systemctl stop boinc-client
    sudo rm -f /etc/systemd/system/boinc-client.service.d/affinity-user.conf
    sudo chown -R boinc:boinc "$DATA_DIR"
    sudo systemctl daemon-reload
    sudo systemctl start boinc-client

The permission error may return after undoing the workaround. Do not run
PrimeGrid Affinity as root.
