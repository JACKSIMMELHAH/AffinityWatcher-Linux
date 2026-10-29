PrimeGrid Affinity v6
====================

INSTALLATION
------------
Requirements: Python 3.8+ and Tkinter.

Ubuntu / Debian / Zorin:

    sudo apt install python3 python3-tk

1. Extract the ZIP.
2. Open a terminal in that folder and run:

       bash install.sh
       
3. Launch "PrimeGrid Affinity" from the application menu.


TASK IDENTIFICATION
------------------
Only active CPU PrimeGrid tasks are shown. The app uses BOINC's local GUI RPC
plan-class/resource metadata to hide GPU tasks.

If local BOINC GUI RPC is unavailable or a worker cannot be matched safely,
that worker is hidden.

AUTOMATIC MONITOR STARTUP
-------------------------
The installer creates this startup entry:

    ~/.config/autostart/primegrid-affinity-monitor.desktop

Disable automatic startup:

    mv ~/.config/autostart/primegrid-affinity-monitor.desktop \
       ~/.config/autostart/primegrid-affinity-monitor.desktop.disabled

Re-enable it by removing the .disabled suffix. 

The monitor log is located at:

    ~/.local/state/primegrid-affinity-gui/monitor.log

BOINC PERMISSION FIX (RECOMMENDED)
----------------------------------
If applying affinity fails with "Operation not permitted" because BOINC tasks
run under the separate `boinc` account, install the restricted helper from the
same extracted folder:

    sudo bash install-affinity-helper.sh

REMOVE THE HELPER
-----------------
    sudo rm -f "/etc/sudoers.d/primegrid-affinity-$(id -u)"
    sudo rm -f /usr/local/libexec/primegrid-affinity-helper.py
