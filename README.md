# AffinityWatcherZorin

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
1. Extract primegrid-affinity-gui-v5.zip.
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

Buttons
-------
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
