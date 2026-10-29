PrimeGrid Affinity v5
====================

INSTALLATION
------------
Requirements: Python 3.8+ and Tkinter.

Ubuntu / Debian / Zorin:
    sudo apt install python3 python3-tk

Arch Linux:
    sudo pacman -S python tk

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

Choose the correct data directory below. Do not continue unless the service
exists and you have confirmed the directory for your installation.

Ubuntu / Debian / Zorin:
    DATA_DIR=/var/lib/boinc-client

Arch Linux (use this instead):
    DATA_DIR=/var/lib/boinc

Then run the following commands as your normal desktop user, not from a root
shell. Keep the DATA_DIR line you selected immediately before these commands:

    u="$(id -un)"
    g="$(id -gn)"
    sudo systemctl stop boinc-client
    sudo install -d /etc/systemd/system/boinc-client.service.d
    printf '[Service]\nUser=%s\nGroup=%s\nSupplementaryGroups=boinc\n' "$u" "$g" | \
      sudo tee /etc/systemd/system/boinc-client.service.d/affinity-user.conf >/dev/null
    sudo chown -R "$u:$g" "$DATA_DIR"
    sudo systemctl daemon-reload
    sudo systemctl start boinc-client

This workaround was tested on Zorin only. The Arch commands are provided as a
starting point and have not been runtime-tested. If the service's user, group,
unit name, or data directory differs, do not copy these commands unchanged.

UNDO THE WORKAROUND
-------------------
Set DATA_DIR to the same directory you used above, then run:

    sudo systemctl stop boinc-client
    sudo rm -f /etc/systemd/system/boinc-client.service.d/affinity-user.conf
    sudo chown -R boinc:boinc "$DATA_DIR"
    sudo systemctl daemon-reload
    sudo systemctl start boinc-client

The permission error may return after undoing the workaround. Do not run
PrimeGrid Affinity as root.
