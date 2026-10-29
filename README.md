PrimeGrid Affinity v5
====================

INSTALLATION
------------
Requirements: Python 3.8+ and Tkinter.

Ubuntu / Debian / Zorin:

    sudo apt install python3 python3-tk

1. Extract the ZIP.
2. Open a terminal in that folder and run:
   
       bash install.sh
   
4. Launch "PrimeGrid Affinity" from the application menu.

BOINC PERMISSION FIX (RECOMMENDED)
----------------------------------
If applying affinity fails with "Operation not permitted" because BOINC tasks
run under the separate `boinc` account, install the helper from the
same extracted folder:

    sudo bash install-affinity-helper.sh

REMOVE THE HELPER
-----------------
    sudo rm -f "/etc/sudoers.d/primegrid-affinity-$(id -u)"
    sudo rm -f /usr/local/libexec/primegrid-affinity-helper.py

