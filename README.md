PrimeGrid Affinity GUI v5 — refreshed interface
================================================

Purpose
-------
A graphical Linux/Zorin utility for assigning PrimeGrid application processes
to CPU pools on detected L3/CCD domains. It supports multiple placement slots,
including multiple simultaneous instances of the same executable, and stores
profiles locally.

What's new in v5
----------------
- Refreshed, modernized interface with clearer sections, typography, and action buttons.
- Task and profile tables have more spacing, alternating row shading, and horizontal
  scrollbars so long application names or CPU lists don't collide with neighboring columns.
- Settings are arranged in a compact row to leave more room for task/profile details.
- Saved profiles and the activity log are in separate tabs.
- Activity log entries are easier to scan with timestamps and severity colors.

Install / update
----------------
1. Extract AffinityWatcherZorin-5.zip.
2. Open Terminal in the extracted folder and run:
       bash install.sh
3. If Tkinter is missing:
       sudo apt install python3-tk
   Then run bash install.sh again.
4. Open Zorin's application menu and search for "PrimeGrid Affinity".

The installer places the app in ~/.local/bin and a desktop entry in
~/.local/share/applications. It does not modify system-wide files.

If you already have the background monitor running from a previous version,
open the new GUI and choose Stop monitor, then Start / update monitor. The
new interface can be used without changing your saved profile file.

How multiple tasks work
-----------------------
- Refresh tasks to see each detected PrimeGrid application process.
- Select a running task, choose a placement slot (1–8), CCD/L3 domain, and
  physical-core limit.
- "Save & apply selected task" saves that executable/slot profile, assigns
  the selected live process to the slot, and applies affinity immediately.
- "Save profile only" saves the chosen slot for the selected executable but
  does not change any running task or explicitly bind the selected process.
- Configure slot 1 for CCD/L3 1 and slot 2 for CCD/L3 2 to run two instances
  of the same executable on separate CCDs. Slots are independent per
  executable, so different PrimeGrid applications can each have their own
  slot 1, slot 2, etc.
- "Start / update monitor" starts one monitor for all saved profiles. It
  reloads profile settings every two seconds, follows process identities while
  tasks run, and frees slots when tasks end. New instances of the same
  executable are assigned to a free configured slot.
- If more simultaneous instances are running than there are saved slots for
  that executable, extra instances are left untouched rather than guessed.
- The monitor continues after the GUI closes. The log is at:
    ~/.local/state/primegrid-affinity-gui/monitor.log
- Saved configuration is at:
    ~/.local/state/primegrid-affinity-gui/settings.json

Meaning of core count
---------------------
The core count is the number of physical cores in the chosen CCD/L3 domain
that the application process is allowed to use. The program picks one logical
CPU per physical core and sets that same CPU pool for every Linux thread in
the selected application process. Threads can be scheduled on any CPU in that
pool; this is a CPU affinity limit, not a one-thread-per-core binding.

It does not change BOINC's task thread count. For example, eight calculation
threads restricted to four physical cores will share those four cores. For
two eight-thread tasks each restricted to eight different cores on separate
CCDs, each task gets its own eight-core pool. The core selector is capped by
the selected CCD's physical core count and the selected task's current Linux
thread count.

Task discovery and limitations
------------------------------
The app searches local /proc process information for leaf processes whose
executable path is inside BOINC's projects/www.primegrid.com directory. This
works for application processes whose executable paths are visible there;
not every wrapper or future BOINC app is guaranteed to be detectable. For GPU
tasks, CPU affinity affects host-side CPU threads only; it does not pin or
change GPU computation.

The program reads CPU topology from /sys/devices/system/cpu and refuses to
apply when the selected topology/core count is invalid. If an affinity
assignment fails partway through, it attempts to restore the previous masks.
Changing affinity can reduce performance, so compare equivalent work-unit
progress before choosing a permanent layout. This tool does not change the
BIOS, BOINC thread-count configuration, or system-wide CPU settings.

BOINC permission issue (native package on Zorin/Ubuntu)
-------------------------------------------------------
~~~~~~~~
The GUI detects a PrimeGrid task, but applying affinity fails with an error like:

    [Errno 1] Operation not permitted

This commonly occurs when the native boinc-client service runs calculations as
the separate `boinc` system account while PrimeGrid Affinity runs as your desktop
user. Linux normally prevents an unprivileged user from changing the CPU affinity
of another user's process.

First, confirm that you are using the native APT package, not the Flatpak build:

    systemctl status boinc-client --no-pager

The native package normally has a `boinc-client.service`. The Flatpak build has a
different runtime model and may not provide this service.

Workaround (changes the BOINC service account)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The following workaround was tested on a local Zorin installation. It runs the
BOINC client under your normal desktop user so the GUI and BOINC calculation
processes have the same user identity. This weakens BOINC's normal security
isolation: project applications may be able to access files available to your
user. Use this only on a personal machine where you accept that trade-off; a
narrowly scoped privileged helper for affinity changes would be safer.

Before proceeding, close BOINC Manager. Stopping the service may interrupt active
work; allow tasks to checkpoint first if you want to minimize lost progress.
Run these commands from a terminal opened as your normal desktop user (do not
open a root shell first):

    u="$(id -un)"
    g="$(id -gn)"

    sudo systemctl stop boinc-client

    sudo install -d /etc/systemd/system/boinc-client.service.d

    printf '[Service]\nUser=%s\nGroup=%s\nSupplementaryGroups=boinc\n' "$u" "$g" | \
      sudo tee /etc/systemd/system/boinc-client.service.d/affinity-user.conf >/dev/null

    sudo chown -R "$u:$g" /var/lib/boinc-client
    sudo systemctl daemon-reload
    sudo systemctl start boinc-client

This creates a systemd service override, changes the ownership of the BOINC data
directory to your user, reloads systemd, and starts BOINC again. It does not edit
the PrimeGrid Affinity profile file. The saved profiles remain in:

    ~/.local/state/primegrid-affinity-gui/settings.json

To verify the service identity:

    systemctl show boinc-client -p User -p Group -p SupplementaryGroups

Then reopen PrimeGrid Affinity, refresh the task list, and check the Activity log.
The monitor log is:

    ~/.local/state/primegrid-affinity-gui/monitor.log

If it still reports `Operation not permitted`, do not keep repeating the ownership
change; inspect the exact log message and service status first.

Revert the workaround
~~~~~~~~~~~~~~~~~~~~~
To restore the usual dedicated `boinc` service account on a standard Zorin/Ubuntu
APT installation, stop the service, remove the override, restore ownership of the
BOINC data directory, reload systemd, and start the service:

    sudo systemctl stop boinc-client
    sudo rm -f /etc/systemd/system/boinc-client.service.d/affinity-user.conf
    sudo chown -R boinc:boinc /var/lib/boinc-client
    sudo systemctl daemon-reload
    sudo systemctl start boinc-client

This re-enables the default service identity. The override directory can be removed
if empty:

    sudo rmdir /etc/systemd/system/boinc-client.service.d 2>/dev/null || true

After reverting, the permission error may return because BOINC's tasks once again
run under a different account. Do not run the entire GUI as root as a workaround.
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Buttons

- Refresh tasks: discover live PrimeGrid processes.
- Save & apply selected task: save the selected slot, bind the current
  process to it, and apply the chosen CPU pool.
- Save profile only: save a slot without changing a running task.
- Start / update monitor: start the global monitor or leave an existing
  current-version monitor running; saved profiles are reloaded automatically.
- Check status: show current process affinity and saved profiles in the activity tab.
- Stop monitor: stop automatic changes but do not restore existing affinity.
- Restore selected task: stop the monitor (after confirmation, if running) and
  restore the selected task to the CPU set allowed by its parent process.
