#!/usr/bin/env python3
"""PrimeGrid multi-task CPU affinity GUI for Linux/Zorin.

Profiles are saved per PrimeGrid executable and placement slot. Multiple live
instances of the same executable can use separate slots/CCDs. A background
monitor follows configured tasks, remembering the slot assigned to each live
process identity and freeing it when that process exits.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import signal

APP_NAME = "PrimeGrid Affinity"
SYS = Path("/sys/devices/system/cpu")
PROC = Path("/proc")
STATE_DIR = Path.home() / ".local/state/primegrid-affinity-gui"
PID_FILE = STATE_DIR / "monitor.pid"
LOG_FILE = STATE_DIR / "monitor.log"
CONFIG_FILE = STATE_DIR / "settings.json"
LOCK_FILE = STATE_DIR / "settings.lock"
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_MARKER = "projects/www.primegrid.com/"
MAX_SLOTS = 8
CONFIG_VERSION = 3


@dataclass
class Task:
    pid: int
    name: str
    command: str
    tids: list[int]
    ppid: int
    start_ticks: int

    @property
    def identity(self) -> str:
        return f"{self.pid}:{self.start_ticks}"

    @property
    def label(self) -> str:
        return f"{self.name} — PID {self.pid} — {len(self.tids)} threads"


def parse_cpus(value: str) -> set[int]:
    result: set[int] = set()
    for part in value.strip().split(","):
        if not part:
            continue
        fields = part.split("-")
        if len(fields) == 1:
            result.add(int(fields[0]))
        else:
            start, end = int(fields[0]), int(fields[1])
            step = int(fields[2]) if len(fields) > 2 else 1
            result.update(range(start, end + 1, step))
    return result


def fmt_cpus(cpus) -> str:
    items = sorted(set(cpus))
    if not items:
        return "(none)"
    parts = []
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1] == items[j] + 1:
            j += 1
        parts.append(str(items[i]) if i == j else f"{items[i]}-{items[j]}")
        i = j + 1
    return ",".join(parts)


def read_stat(pid: int):
    text = (PROC / str(pid) / "stat").read_text()
    fields = text.rsplit(")", 1)[1].split()
    # fields[0] is original stat field 3 (state); PPID is original field 4.
    # Starttime is original field 22, therefore index 19 here.
    return int(fields[1]), int(fields[19])


def read_ppid(pid: int) -> int:
    return read_stat(pid)[0]


def cpu_topology():
    """Return L3-sharing domains, each with physical-core sibling tuples."""
    online = parse_cpus((SYS / "online").read_text())
    domains: set[frozenset[int]] = set()
    for cpu in online:
        for index in (SYS / f"cpu{cpu}" / "cache").glob("index*"):
            try:
                if (index / "level").read_text().strip() != "3":
                    continue
                shared = parse_cpus((index / "shared_cpu_list").read_text()) & online
                if shared:
                    domains.add(frozenset(shared))
            except (OSError, ValueError):
                continue

    result = []
    covered = set()
    for domain in sorted(domains, key=min):
        cores = set()
        for cpu in domain:
            try:
                siblings = parse_cpus(
                    (SYS / f"cpu{cpu}" / "topology" / "thread_siblings_list").read_text()
                ) & set(domain)
                if siblings:
                    cores.add(tuple(sorted(siblings)))
            except (OSError, ValueError):
                continue
        core_list = sorted(cores, key=lambda c: min(c))
        if not core_list:
            continue
        if covered.intersection(domain):
            raise RuntimeError("Detected overlapping L3 CPU domains. No changes made.")
        covered.update(domain)
        result.append((set(domain), core_list))

    if not result:
        raise RuntimeError("Could not detect any L3/CCD domains. No changes made.")
    return sorted(result, key=lambda item: min(item[0]))


def targets_for(domains, ccd_index: int, core_count: int) -> set[int]:
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    cores = domains[ccd_index][1]
    if core_count < 1 or core_count > len(cores):
        raise RuntimeError(f"Choose between 1 and {len(cores)} physical cores for this CCD.")
    # One logical CPU per physical core; avoid using both SMT siblings.
    return {core[0] for core in cores[:core_count]}


def _exe_path(pid: int, argv0: str) -> str:
    if PROJECT_MARKER in argv0.replace("\\", "/"):
        return argv0
    try:
        resolved = str((PROC / str(pid) / "exe").resolve())
        if PROJECT_MARKER in resolved.replace("\\", "/"):
            return resolved
    except OSError:
        pass
    return ""


def _load_candidate(pid: int):
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
        argv = [x.decode(errors="replace") for x in raw.split(b"\0") if x]
        if not argv:
            return None
        exe = _exe_path(pid, argv[0])
        if not exe:
            return None
        name = Path(exe).name
        if not name or name in {"boinc", "boincmgr", "boinc_client"}:
            return None
        tids = sorted(int(item.name) for item in (PROC / str(pid) / "task").iterdir() if item.name.isdigit())
        if not tids:
            return None
        ppid, start_ticks = read_stat(pid)
        return Task(pid, name, " ".join(argv), tids, ppid, start_ticks)
    except (OSError, ValueError, ProcessLookupError):
        return None


def _is_ancestor(ancestor_pid: int, descendant: Task) -> bool:
    current = descendant.ppid
    seen = set()
    while current > 1 and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        try:
            current = read_ppid(current)
        except (OSError, ValueError, ProcessLookupError):
            break
    return False


def find_tasks() -> list[Task]:
    candidates = []
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            task = _load_candidate(int(entry.name))
            if task:
                candidates.append(task)
    # Keep leaf calculation workers rather than wrappers around them.
    leaves = [task for task in candidates if not any(
        other.pid != task.pid and _is_ancestor(task.pid, other) for other in candidates
    )]
    return sorted(leaves, key=lambda t: (t.name.lower(), t.start_ticks, t.pid))


def find_task_by_identity(identity: str) -> Task:
    for task in find_tasks():
        if task.identity == identity:
            return task
    raise RuntimeError("The selected PrimeGrid task has ended or restarted. Refresh the task list.")


def apply_task_affinity(identity: str, ccd_index: int, core_count: int):
    domains = cpu_topology()
    task = find_task_by_identity(identity)
    tids = sorted(int(item.name) for item in (PROC / str(task.pid) / "task").iterdir() if item.name.isdigit())
    if not tids:
        raise RuntimeError("The task no longer has live threads. No changes made.")
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    if core_count < 1:
        raise RuntimeError("Choose at least 1 physical core.")
    # A task cannot usefully be assigned more CPU slots than it currently has
    # Linux threads. Also never exceed the number of cores in the chosen domain.
    effective_count = min(int(core_count), len(tids), len(domains[ccd_index][1]))
    allowed = targets_for(domains, ccd_index, effective_count)
    old = {}
    for tid in tids:
        old[tid] = os.sched_getaffinity(tid)
    try:
        for tid in tids:
            os.sched_setaffinity(tid, allowed)
        for tid in tids:
            if os.sched_getaffinity(tid) != allowed:
                raise RuntimeError(f"Affinity verification failed for TID {tid}.")
    except (OSError, ProcessLookupError, RuntimeError) as exc:
        for tid, mask in old.items():
            try:
                os.sched_setaffinity(tid, mask)
            except OSError:
                pass
        raise RuntimeError(f"Could not safely apply affinity; rollback attempted: {exc}") from exc
    return task, tids, allowed, domains


def restore_parent_affinity(identity: str):
    task = find_task_by_identity(identity)
    tids = sorted(int(item.name) for item in (PROC / str(task.pid) / "task").iterdir() if item.name.isdigit())
    ppid = read_ppid(task.pid)
    if ppid <= 1:
        raise RuntimeError("Could not determine the task's parent process. No changes made.")
    allowed = os.sched_getaffinity(ppid)
    if not allowed:
        raise RuntimeError("The parent process has an empty allowed CPU set. No changes made.")
    before = {tid: os.sched_getaffinity(tid) for tid in tids}
    try:
        for tid in tids:
            os.sched_setaffinity(tid, allowed)
        for tid in tids:
            if os.sched_getaffinity(tid) != allowed:
                raise RuntimeError(f"Could not verify restored affinity for TID {tid}.")
    except (OSError, ProcessLookupError, RuntimeError) as exc:
        for tid, mask in before.items():
            try:
                os.sched_setaffinity(tid, mask)
            except OSError:
                pass
        raise RuntimeError(f"Restore failed; rollback attempted: {exc}") from exc
    return task, ppid, allowed


def default_config():
    return {"version": CONFIG_VERSION, "apps": {}, "last_selection": {}}


def _read_config_unlocked():
    if not CONFIG_FILE.exists():
        return default_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read saved settings at {CONFIG_FILE}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("apps", {}), dict):
        raise RuntimeError(f"Saved settings file has an unexpected format: {CONFIG_FILE}")
    data.setdefault("version", CONFIG_VERSION)
    data.setdefault("apps", {})
    data.setdefault("last_selection", {})
    return data


def _write_config_unlocked(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data["version"] = CONFIG_VERSION
    fd, tmp_name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, CONFIG_FILE)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_config():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            return _read_config_unlocked()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def update_config(mutator):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = _read_config_unlocked()
            result = mutator(data)
            _write_config_unlocked(data)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def app_profile(config, app_name: str):
    apps = config.setdefault("apps", {})
    profile = apps.setdefault(app_name, {"slots": {}, "assignments": {}})
    profile.setdefault("slots", {})
    profile.setdefault("assignments", {})
    return profile


def slot_sort_key(slot_id):
    try:
        return (0, int(slot_id))
    except (TypeError, ValueError):
        return (1, str(slot_id))


def current_cpu_text(task: Task) -> str:
    masks = set()
    for tid in task.tids:
        try:
            masks.add(fmt_cpus(os.sched_getaffinity(tid)))
        except (OSError, ProcessLookupError):
            pass
    if not masks:
        return "unavailable"
    if len(masks) == 1:
        return next(iter(masks))
    return "mixed masks"


def save_assignment(task: Task, slot_id: str, ccd_index: int, core_count: int, live_tasks: list[Task]):
    live_identities = {t.identity for t in live_tasks if t.name == task.name}
    def mutate(data):
        profile = app_profile(data, task.name)
        assignments = profile["assignments"]
        # Remove stale assignments for this executable and detect occupied slots.
        for identity in list(assignments):
            if identity not in live_identities:
                assignments.pop(identity, None)
        for other_identity, other_slot in assignments.items():
            if other_identity != task.identity and str(other_slot) == str(slot_id):
                raise RuntimeError(
                    f"Placement slot {slot_id} is already assigned to another running {task.name} task. "
                    "Choose another slot so each task gets an independent profile."
                )
        profile["slots"][str(slot_id)] = {"ccd_index": int(ccd_index), "core_count": int(core_count)}
        assignments[task.identity] = str(slot_id)
        data["last_selection"] = {"app_name": task.name, "slot_id": str(slot_id)}
    update_config(mutate)


def save_profile_only(app_name: str, slot_id: str, ccd_index: int, core_count: int):
    def mutate(data):
        profile = app_profile(data, app_name)
        profile["slots"][str(slot_id)] = {"ccd_index": int(ccd_index), "core_count": int(core_count)}
        data["last_selection"] = {"app_name": app_name, "slot_id": str(slot_id)}
    update_config(mutate)


def log_line(message: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def monitor_status():
    if not PID_FILE.exists():
        return "Background monitor: not running."
    try:
        pid = int(PID_FILE.read_text().strip())
        raw = (PROC / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        if str(SCRIPT_PATH) in raw and "--monitor" in raw:
            return f"Background monitor: running (PID {pid}). Log: {LOG_FILE}"
    except (OSError, ValueError):
        pass
    return "Background monitor: not running (stale PID file)."


def monitor_pid_and_argv():
    if not PID_FILE.exists():
        return None, []
    try:
        pid = int(PID_FILE.read_text().strip())
        raw = (PROC / str(pid) / "cmdline").read_bytes()
        argv = [x.decode(errors="replace") for x in raw.split(b"\0") if x]
        if str(SCRIPT_PATH) not in argv and not any(Path(a).resolve() == SCRIPT_PATH for a in argv if a.startswith("/")):
            return None, []
        if "--monitor" not in argv:
            return None, []
        return pid, argv
    except (OSError, ValueError, ProcessLookupError):
        return None, []


def stop_managed_monitor():
    pid, argv = monitor_pid_and_argv()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return "No active GUI-managed monitor found; cleared stale PID file if present."
    try:
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return f"Stopped background monitor (PID {pid})."
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        return "Background monitor was already stopped."
    except PermissionError:
        return "Could not stop the monitor. Run the GUI as the same Linux user that started it."


def _persist_assignments(app_name: str, assignments: dict[str, str]):
    def mutate(data):
        profile = data.get("apps", {}).get(app_name)
        if not isinstance(profile, dict):
            return
        slots = profile.get("slots", {})
        profile["assignments"] = {
            identity: str(slot)
            for identity, slot in assignments.items()
            if str(slot) in slots
        }
    update_config(mutate)


def monitor_loop():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    log_line("Multi-task background monitor started.")
    last_status = {}
    while True:
        try:
            config = load_config()
            tasks = find_tasks()
            by_app = {}
            for task in tasks:
                by_app.setdefault(task.name, []).append(task)

            configured_apps = config.get("apps", {})
            for app_name, profile in configured_apps.items():
                slots = profile.get("slots", {}) if isinstance(profile, dict) else {}
                if not isinstance(slots, dict) or not slots:
                    continue
                app_tasks = sorted(by_app.get(app_name, []), key=lambda t: (t.start_ticks, t.pid))
                live_ids = {t.identity for t in app_tasks}
                prior = profile.get("assignments", {}) if isinstance(profile, dict) else {}
                if not isinstance(prior, dict):
                    prior = {}
                slot_ids = sorted((str(s) for s in slots), key=slot_sort_key)
                new_assignments = {}
                used_slots = set()
                task_by_id = {t.identity: t for t in app_tasks}

                # Honor explicit saved mappings first, provided each slot is unique.
                for identity, slot in prior.items():
                    slot = str(slot)
                    if identity in live_ids and slot in slots and slot not in used_slots:
                        new_assignments[identity] = slot
                        used_slots.add(slot)

                # Assign unconfigured task instances to any free profile slot.
                for task in app_tasks:
                    if task.identity in new_assignments:
                        continue
                    free = next((s for s in slot_ids if s not in used_slots), None)
                    if free is None:
                        continue
                    new_assignments[task.identity] = free
                    used_slots.add(free)

                if new_assignments != {str(k): str(v) for k, v in prior.items()}:
                    _persist_assignments(app_name, new_assignments)

                for identity, slot in new_assignments.items():
                    task = task_by_id.get(identity)
                    settings = slots.get(slot)
                    if task is None or not isinstance(settings, dict):
                        continue
                    try:
                        ccd_index = int(settings["ccd_index"])
                        core_count = int(settings["core_count"])
                        domains = cpu_topology()
                        tids = sorted(int(x.name) for x in (PROC / str(task.pid) / "task").iterdir() if x.name.isdigit())
                        if not tids:
                            continue
                        if ccd_index < 0 or ccd_index >= len(domains):
                            raise RuntimeError("The selected CCD/L3 domain no longer exists.")
                        effective_count = min(core_count, len(tids), len(domains[ccd_index][1]))
                        target = targets_for(domains, ccd_index, effective_count)
                        needs_change = any(os.sched_getaffinity(tid) != target for tid in tids)
                        if needs_change:
                            current, applied_tids, allowed, _ = apply_task_affinity(identity, ccd_index, core_count)
                            msg = (f"Applied {app_name} PID {current.pid} to slot {slot}, CCD/L3 {ccd_index + 1}, "
                                   f"{len(allowed)} physical core(s); allowed CPUs {fmt_cpus(allowed)}; "
                                   f"{len(applied_tids)} Linux thread(s).")
                            if len(allowed) < core_count:
                                msg += f" Profile requested {core_count} cores; capped to {len(allowed)} for this task's thread count/topology."
                            if last_status.get(identity) != msg:
                                log_line(msg)
                            last_status[identity] = msg
                        else:
                            last_status.pop(identity, None)
                    except (OSError, ProcessLookupError, RuntimeError, KeyError, ValueError) as exc:
                        note = f"No affinity change for {app_name} PID {task.pid}: {exc}"
                        if last_status.get(identity) != note:
                            log_line(note)
                        last_status[identity] = note

                for task in app_tasks:
                    if task.identity not in new_assignments:
                        note = (f"{app_name} PID {task.pid} not managed: no free configured slot. "
                                f"Add another slot profile to manage more simultaneous instances.")
                        if last_status.get(task.identity) != note:
                            log_line(note)
                        last_status[task.identity] = note

            active_ids = {t.identity for t in tasks}
            last_status = {k: v for k, v in last_status.items() if k in active_ids}
        except Exception as exc:
            note = f"Monitor loop error; no additional affinity changes this pass: {exc}"
            if last_status.get("__error__") != note:
                log_line(note)
            last_status["__error__"] = note
        time.sleep(2)


def run_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print("Tkinter is missing. Install it with: sudo apt install python3-tk", file=sys.stderr)
        raise SystemExit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.title(f"{APP_NAME} · v5")
    root.geometry("1180x900")
    root.minsize(1000, 760)
    root.configure(background="#f3f5fa")

    # Use a predictable ttk theme so the interface looks consistent on GTK-based desktops.
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    BG = "#f3f5fa"
    SURFACE = "#ffffff"
    TEXT = "#182230"
    MUTED = "#667085"
    BORDER = "#d9e0ec"
    ACCENT = "#4f46e5"
    ACCENT_DARK = "#4338ca"
    GREEN = "#0f766e"
    GREEN_DARK = "#115e59"
    RED = "#b42318"
    RED_DARK = "#912018"

    style.configure("App.TFrame", background=BG)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("Hint.TFrame", background="#f5f7ff")
    style.configure("TLabel", background=BG, foreground=TEXT, font=("Sans", 10))
    style.configure("Card.TLabel", background=SURFACE, foreground=TEXT, font=("Sans", 10))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Sans", 10))
    style.configure("Header.TLabel", background=BG, foreground=TEXT, font=("Sans", 23, "bold"))
    style.configure("Subheader.TLabel", background=BG, foreground=MUTED, font=("Sans", 10))
    style.configure("Badge.TLabel", background="#e8eaff", foreground=ACCENT_DARK,
                    font=("Sans", 9, "bold"), padding=(10, 5))
    style.configure("Hint.TLabel", background="#f5f7ff", foreground="#475467",
                    font=("Sans", 10), padding=12)
    style.configure("Status.TFrame", background="#eaf0ff")
    style.configure("Status.TLabel", background="#eaf0ff", foreground="#3730a3",
                    font=("Sans", 10, "bold"), padding=(12, 9))
    style.configure("Card.TLabelframe", background=SURFACE, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, borderwidth=1, relief="solid", padding=12)
    style.configure("Card.TLabelframe.Label", background=SURFACE, foreground="#344054",
                    font=("Sans", 10, "bold"))
    style.configure("TButton", background=SURFACE, foreground=TEXT, bordercolor=BORDER,
                    padding=(12, 8), font=("Sans", 10, "bold"), focuscolor=ACCENT)
    style.map("TButton", background=[("pressed", "#e7ebf3"), ("active", "#f0f3f9")])
    style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT)
    style.map("Primary.TButton", background=[("pressed", ACCENT_DARK), ("active", ACCENT_DARK)],
              foreground=[("disabled", "#e5e7eb")])
    style.configure("Success.TButton", background=GREEN, foreground="#ffffff", bordercolor=GREEN)
    style.map("Success.TButton", background=[("pressed", GREEN_DARK), ("active", GREEN_DARK)])
    style.configure("Danger.TButton", background="#fff5f5", foreground=RED, bordercolor="#fecaca")
    style.map("Danger.TButton", background=[("pressed", "#fee2e2"), ("active", "#fee2e2")])
    style.configure("TCombobox", padding=(8, 6), fieldbackground="#ffffff", background="#ffffff",
                    foreground=TEXT, arrowcolor=MUTED)
    style.configure("TSpinbox", padding=(7, 5), fieldbackground="#ffffff", background="#ffffff",
                    foreground=TEXT, arrowcolor=MUTED)
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                    borderwidth=0, relief="flat", rowheight=32, font=("Sans", 10))
    style.configure("Treeview.Heading", background="#eef2f8", foreground="#475467",
                    relief="flat", padding=(10, 9), font=("Sans", 9, "bold"))
    style.map("Treeview.Heading", background=[("active", "#e4eaf4")])
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
    style.configure("Vertical.TScrollbar", background="#e4e7ec", troughcolor="#f8fafc", bordercolor="#f8fafc")
    style.configure("Horizontal.TScrollbar", background="#e4e7ec", troughcolor="#f8fafc", bordercolor="#f8fafc")
    style.configure("Modern.TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("Modern.TNotebook.Tab", background="#e9edf5", foreground="#475467",
                    padding=(16, 9), font=("Sans", 10, "bold"))
    style.map("Modern.TNotebook.Tab", background=[("selected", SURFACE), ("active", "#e0e7f2")],
              foreground=[("selected", ACCENT_DARK)])

    outer = ttk.Frame(root, style="App.TFrame", padding=(18, 12))
    outer.pack(fill="both", expand=True)

    header = ttk.Frame(outer, style="App.TFrame")
    header.pack(fill="x", pady=(0, 4))
    header_left = ttk.Frame(header, style="App.TFrame")
    header_left.pack(side="left", fill="x", expand=True)
    ttk.Label(header_left, text="PrimeGrid Affinity", style="Header.TLabel").pack(anchor="w")
    ttk.Label(
        header_left,
        text="Manage CPU placement for PrimeGrid tasks across your CCDs.",
        style="Subheader.TLabel",
    ).pack(anchor="w", pady=(3, 0))
    ttk.Label(header, text="LOCAL CPU CONTROL", style="Badge.TLabel").pack(side="right", anchor="n", pady=(4, 0))

    status_var = tk.StringVar(value="Refresh tasks to begin.")
    status_frame = ttk.Frame(outer, style="Status.TFrame")
    status_frame.pack(fill="x", pady=(12, 14))
    status_label = ttk.Label(status_frame, textvariable=status_var, style="Status.TLabel", wraplength=1080, anchor="w")
    status_label.pack(fill="x")

    task_frame = ttk.LabelFrame(outer, text="  RUNNING PRIMEGRID TASKS  ", style="Card.TLabelframe", padding=8)
    task_frame.pack(fill="x", pady=(0, 12))
    task_frame.columnconfigure(0, weight=1)
    task_frame.rowconfigure(0, weight=1)
    columns = ("app", "pid", "threads", "allowed", "slot")
    tasks_tree = ttk.Treeview(task_frame, columns=columns, show="headings", height=4, selectmode="browse")
    for col, title, width, stretch in [
        ("app", "Application", 380, True), ("pid", "PID", 85, False),
        ("threads", "Linux threads", 110, False), ("allowed", "Allowed CPUs", 245, True),
        ("slot", "Saved slot", 100, False),
    ]:
        tasks_tree.heading(col, text=title)
        tasks_tree.column(col, width=width, minwidth=65 if col != "app" else 180,
                          stretch=stretch, anchor="w")
    tasks_tree.tag_configure("even", background="#ffffff")
    tasks_tree.tag_configure("odd", background="#f8faff")
    tasks_tree.grid(row=0, column=0, sticky="nsew")
    task_scroll = ttk.Scrollbar(task_frame, orient="vertical", command=tasks_tree.yview)
    task_scroll.grid(row=0, column=1, sticky="ns")
    task_xscroll = ttk.Scrollbar(task_frame, orient="horizontal", command=tasks_tree.xview)
    task_xscroll.grid(row=1, column=0, sticky="ew", pady=(2, 0))
    tasks_tree.configure(yscrollcommand=task_scroll.set, xscrollcommand=task_xscroll.set)
    ttk.Button(task_frame, text="↻  Refresh tasks", style="TButton",
               command=lambda: refresh_tasks()).grid(row=2, column=0, sticky="w", pady=(9, 0))

    setting_frame = ttk.LabelFrame(outer, text="  TASK SETTINGS  ", style="Card.TLabelframe", padding=8)
    setting_frame.pack(fill="x", pady=(0, 12))
    setting_frame.columnconfigure(3, weight=1)

    ttk.Label(setting_frame, text="Placement slot", style="Card.TLabel").grid(
        row=0, column=0, sticky="w", padx=(2, 8), pady=(2, 6))
    slot_var = tk.StringVar(value="1")
    slot_combo = ttk.Combobox(setting_frame, textvariable=slot_var, state="readonly",
                              values=[str(i) for i in range(1, MAX_SLOTS + 1)], width=7)
    slot_combo.grid(row=0, column=1, sticky="w", padx=(0, 18), pady=(2, 6))

    ttk.Label(setting_frame, text="CCD / L3 domain", style="Card.TLabel").grid(
        row=0, column=2, sticky="w", padx=(0, 8), pady=(2, 6))
    ccd_var = tk.StringVar()
    ccd_combo = ttk.Combobox(setting_frame, textvariable=ccd_var, state="readonly", width=42)
    ccd_combo.grid(row=0, column=3, sticky="ew", padx=(0, 18), pady=(2, 6))

    ttk.Label(setting_frame, text="Physical cores", style="Card.TLabel").grid(
        row=0, column=4, sticky="w", padx=(0, 8), pady=(2, 6))
    core_var = tk.IntVar(value=8)
    core_spin = ttk.Spinbox(setting_frame, from_=1, to=8, textvariable=core_var, width=7, state="readonly")
    core_spin.grid(row=0, column=5, sticky="w", pady=(2, 6))

    hint_frame = ttk.Frame(setting_frame, style="Hint.TFrame")
    hint_frame.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(4, 0))
    profile_hint = ttk.Label(hint_frame, text="Select a running task to configure its placement profile.",
                             style="Hint.TLabel", wraplength=1050, justify="left", anchor="w")
    profile_hint.pack(fill="x")

    actions = ttk.Frame(outer, style="App.TFrame")
    actions.pack(fill="x", pady=(0, 12))
    for col in range(3):
        actions.columnconfigure(col, weight=1, uniform="actions")

    details_notebook = ttk.Notebook(outer, style="Modern.TNotebook")
    details_notebook.pack(fill="both", expand=True, pady=(0, 8))

    saved_tab = ttk.Frame(details_notebook, style="Card.TFrame", padding=8)
    details_notebook.add(saved_tab, text="Saved profiles")
    saved_frame = ttk.LabelFrame(saved_tab, text="  SAVED PLACEMENT PROFILES  ", style="Card.TLabelframe")
    saved_frame.pack(fill="both", expand=True)
    saved_frame.columnconfigure(0, weight=1)
    saved_frame.rowconfigure(0, weight=1)
    saved_columns = ("application", "slot", "ccd", "cores", "assigned")
    saved_tree = ttk.Treeview(saved_frame, columns=saved_columns, show="headings", height=4, selectmode="browse")
    for col, title, width, stretch in [
        ("application", "Application", 365, True), ("slot", "Slot", 75, False),
        ("ccd", "CCD / L3 domain", 165, False), ("cores", "Physical cores", 125, False),
        ("assigned", "Assigned task PID", 205, True),
    ]:
        saved_tree.heading(col, text=title)
        saved_tree.column(col, width=width, minwidth=70, stretch=stretch, anchor="w")
    saved_tree.tag_configure("even", background="#ffffff")
    saved_tree.tag_configure("odd", background="#f8faff")
    saved_tree.grid(row=0, column=0, sticky="nsew")
    saved_scroll = ttk.Scrollbar(saved_frame, orient="vertical", command=saved_tree.yview)
    saved_scroll.grid(row=0, column=1, sticky="ns")
    saved_xscroll = ttk.Scrollbar(saved_frame, orient="horizontal", command=saved_tree.xview)
    saved_xscroll.grid(row=1, column=0, sticky="ew", pady=(2, 0))
    saved_tree.configure(yscrollcommand=saved_scroll.set, xscrollcommand=saved_xscroll.set)

    activity_tab = ttk.Frame(details_notebook, style="Card.TFrame", padding=8)
    details_notebook.add(activity_tab, text="Activity log")
    log_frame = ttk.LabelFrame(activity_tab, text="  ACTIVITY LOG  ", style="Card.TLabelframe")
    log_frame.pack(fill="both", expand=True)
    log_container = ttk.Frame(log_frame, style="Card.TFrame")
    log_container.pack(fill="both", expand=True)
    output = tk.Text(log_container, height=6, wrap="word", state="disabled",
                     background="#101828", foreground="#e4e7ec", insertbackground="#ffffff",
                     selectbackground="#344054", relief="flat", borderwidth=0,
                     padx=12, pady=10, font=("Monospace", 9), spacing1=2, spacing3=2)
    out_scroll = ttk.Scrollbar(log_container, orient="vertical", command=output.yview)
    output.configure(yscrollcommand=out_scroll.set)
    output.pack(side="left", fill="both", expand=True)
    out_scroll.pack(side="right", fill="y")
    output.tag_configure("timestamp", foreground="#98a2b3")
    output.tag_configure("normal", foreground="#e4e7ec")
    output.tag_configure("success", foreground="#6ce9a6")
    output.tag_configure("warning", foreground="#fdb022")
    output.tag_configure("error", foreground="#fda29b")

    domains = []
    ccd_labels = []
    tasks_by_identity: dict[str, Task] = {}
    selected_app_name = None

    def write(message):
        stamp = datetime.now().strftime("%H:%M:%S")
        lowered = message.lower()
        if any(word in lowered for word in ("failed", "error", "could not", "no affinity change", "not applied")):
            tag = "error"
        elif any(word in lowered for word in ("started", "applied", "restored", "detected")):
            tag = "success"
        elif any(word in lowered for word in ("waiting", "warning", "no primegrid", "not managed")):
            tag = "warning"
        else:
            tag = "normal"
        output.configure(state="normal")
        output.insert("end", f"{stamp}  ", "timestamp")
        output.insert("end", f"{message}\n", tag)
        output.see("end")
        output.configure(state="disabled")
        status_var.set(message)
        # Keep activity discoverable without forcing users to switch tabs every time.
        if any(word in lowered for word in ("failed", "error", "started", "applied", "restored", "monitor")):
            try:
                details_notebook.select(activity_tab)
            except tk.TclError:
                pass

    def selected_task():
        chosen = tasks_tree.selection()
        if not chosen:
            raise RuntimeError("Select a running PrimeGrid task first.")
        identity = chosen[0]
        if identity not in tasks_by_identity:
            raise RuntimeError("The selected task has disappeared. Refresh the task list.")
        return tasks_by_identity[identity]

    def selected_ccd_index():
        try:
            return ccd_labels.index(ccd_var.get())
        except ValueError:
            raise RuntimeError("Select a CCD/L3 domain first.")

    def refresh_core_limit():
        """Cap the core selector at both CCD size and selected task thread count."""
        if not domains or not ccd_labels:
            return
        try:
            ccd_index = selected_ccd_index()
        except RuntimeError:
            ccd_index = 0
        max_cores = len(domains[ccd_index][1])
        try:
            task = selected_task()
        except RuntimeError:
            task = None
        if task is not None:
            max_cores = min(max_cores, len(task.tids))
        max_cores = max(1, max_cores)
        core_spin.configure(from_=1, to=max_cores)
        try:
            current = int(core_var.get())
        except (ValueError, tk.TclError):
            current = max_cores
        core_var.set(min(max(1, current), max_cores))

    def refresh_topology():
        nonlocal domains, ccd_labels
        domains = cpu_topology()
        ccd_labels = [f"CCD/L3 {i + 1} ({len(cores)} physical cores; CPUs {fmt_cpus(domain)})"
                      for i, (domain, cores) in enumerate(domains)]
        previous = ccd_var.get()
        ccd_combo.configure(values=ccd_labels)
        if previous not in ccd_labels:
            ccd_var.set(ccd_labels[0])
        refresh_core_limit()

    def configured_slot(profile):
        assignments = profile.get("assignments", {}) if isinstance(profile, dict) else {}
        # Find selected task's persisted slot if one is available.
        try:
            task = selected_task()
            slot = assignments.get(task.identity)
            if slot is not None:
                return str(slot)
        except RuntimeError:
            pass
        return None

    def default_slot_for_task(task, config):
        profile = config.get("apps", {}).get(task.name, {})
        assigned = profile.get("assignments", {}) if isinstance(profile, dict) else {}
        if task.identity in assigned:
            return str(assigned[task.identity])
        same_app = sorted([t for t in tasks_by_identity.values() if t.name == task.name], key=lambda t: (t.start_ticks, t.pid))
        rank = next((i for i, t in enumerate(same_app) if t.identity == task.identity), 0)
        slots = sorted((str(x) for x in profile.get("slots", {}).keys()), key=slot_sort_key) if isinstance(profile, dict) else []
        if rank < len(slots):
            return slots[rank]
        return str(min(rank + 1, MAX_SLOTS))

    def load_slot_settings():
        try:
            task = selected_task()
        except RuntimeError:
            return
        slot_id = str(slot_var.get())
        try:
            config = load_config()
            profile = config.get("apps", {}).get(task.name, {})
            settings = profile.get("slots", {}).get(slot_id, {}) if isinstance(profile, dict) else {}
            if settings:
                ccd_index = int(settings.get("ccd_index", 0))
                if not (0 <= ccd_index < len(ccd_labels)):
                    ccd_index = 0
                if ccd_labels:
                    ccd_var.set(ccd_labels[ccd_index])
                default_cores = len(domains[ccd_index][1]) if domains else 1
                core_var.set(int(settings.get("core_count", default_cores)))
                profile_hint.configure(
                    text=(f"Saved profile: {task.name}, slot {slot_id}. Settings are stored locally. "
                          f"This task currently has {len(task.tids)} Linux thread(s).")
                )
            else:
                try:
                    numeric = int(slot_id)
                except ValueError:
                    numeric = 1
                ccd_index = (numeric - 1) % max(1, len(domains))
                if domains:
                    ccd_var.set(ccd_labels[ccd_index])
                    core_var.set(len(domains[ccd_index][1]))
                profile_hint.configure(
                    text=(f"No saved profile for {task.name}, slot {slot_id}; defaulting slot {slot_id} "
                          f"to CCD/L3 {ccd_index + 1}. This task currently has {len(task.tids)} Linux thread(s).")
                )
            refresh_core_limit()
        except Exception as exc:
            write(f"Could not load slot settings: {exc}")

    def refresh_saved_profiles():
        for iid in saved_tree.get_children():
            saved_tree.delete(iid)
        try:
            config = load_config()
            tasks = find_tasks()
            live_by_identity = {t.identity: t for t in tasks}
            row_index = 0
            for app_name, profile in sorted(config.get("apps", {}).items()):
                if not isinstance(profile, dict):
                    continue
                slots = profile.get("slots", {})
                assignments = profile.get("assignments", {})
                for slot_id, settings in sorted(slots.items(), key=lambda pair: slot_sort_key(pair[0])):
                    try:
                        ccd_index = int(settings["ccd_index"])
                        cores = int(settings["core_count"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    pids = [str(live_by_identity[identity].pid) for identity, assigned in assignments.items()
                            if str(assigned) == str(slot_id) and identity in live_by_identity and live_by_identity[identity].name == app_name]
                    ccd_text = f"CCD/L3 {ccd_index + 1}"
                    if 0 <= ccd_index < len(domains):
                        ccd_text += f" ({fmt_cpus(domains[ccd_index][0])})"
                    saved_tree.insert(
                        "", "end", iid=f"{app_name}::{slot_id}",
                        values=(app_name, slot_id, ccd_text, cores, ", ".join(pids) if pids else "Available"),
                        tags=("even" if row_index % 2 == 0 else "odd",),
                    )
                    row_index += 1
        except Exception as exc:
            write(f"Could not read saved profiles: {exc}")

    def refresh_tasks(keep_selection=True):
        previous = tasks_tree.selection()[0] if keep_selection and tasks_tree.selection() else None
        try:
            tasks = find_tasks()
            tasks_by_identity.clear()
            tasks_by_identity.update({t.identity: t for t in tasks})
            for iid in tasks_tree.get_children():
                tasks_tree.delete(iid)
            config = load_config()
            for row_index, task in enumerate(tasks):
                profile = config.get("apps", {}).get(task.name, {})
                assignments = profile.get("assignments", {}) if isinstance(profile, dict) else {}
                saved_slot = str(assignments.get(task.identity, "—"))
                tasks_tree.insert(
                    "", "end", iid=task.identity,
                    values=(task.name, task.pid, len(task.tids), current_cpu_text(task), saved_slot),
                    tags=("even" if row_index % 2 == 0 else "odd",),
                )
            if previous in tasks_by_identity:
                tasks_tree.selection_set(previous)
            elif tasks:
                tasks_tree.selection_set(tasks[0].identity)
            if tasks:
                write(f"Detected {len(tasks)} PrimeGrid task instance(s). Select a row to configure it.")
            else:
                write("No PrimeGrid application tasks detected. Start tasks in BOINC and refresh.")
            on_task_selected()
            refresh_saved_profiles()
        except Exception as exc:
            write(f"Task scan failed: {exc}")

    def on_task_selected(_event=None):
        nonlocal selected_app_name
        try:
            task = selected_task()
            selected_app_name = task.name
            config = load_config()
            suggested = default_slot_for_task(task, config)
            slot_var.set(suggested)
            load_slot_settings()
        except RuntimeError:
            selected_app_name = None
            profile_hint.configure(text="Select a detected PrimeGrid task.")

    def on_ccd_change(_event=None):
        refresh_core_limit()

    def on_slot_change(_event=None):
        load_slot_settings()

    tasks_tree.bind("<<TreeviewSelect>>", on_task_selected)
    ccd_combo.bind("<<ComboboxSelected>>", on_ccd_change)
    slot_combo.bind("<<ComboboxSelected>>", on_slot_change)

    def apply_selected(save=True):
        try:
            task = selected_task()
            slot_id = str(slot_var.get())
            ccd_index = selected_ccd_index()
            core_count = int(core_var.get())
            targets_for(cpu_topology(), ccd_index, core_count)
            live_tasks = find_tasks()
            if save:
                save_assignment(task, slot_id, ccd_index, core_count, live_tasks)
            actual, tids, target_cpus, _ = apply_task_affinity(task.identity, ccd_index, core_count)
            write(f"Applied {actual.name} PID {actual.pid} to slot {slot_id}, CCD/L3 {ccd_index + 1}.")
            write(f"Allowed logical CPUs: {fmt_cpus(target_cpus)} ({len(target_cpus)} physical cores), across {len(tids)} Linux thread(s).")
            write("Each thread may run on any CPU in the selected pool; the calculation thread count is unchanged.")
            refresh_tasks()
            refresh_saved_profiles()
        except Exception as exc:
            messagebox.showerror("Affinity not applied", str(exc))
            write(f"No affinity change confirmed: {exc}")

    def save_profile():
        try:
            task = selected_task()
            slot_id = str(slot_var.get())
            ccd_index = selected_ccd_index()
            core_count = int(core_var.get())
            targets_for(cpu_topology(), ccd_index, core_count)
            save_profile_only(task.name, slot_id, ccd_index, core_count)
            write(f"Saved profile for {task.name}, slot {slot_id}, CCD/L3 {ccd_index + 1}, {core_count} core(s). No running task was changed.")
            refresh_saved_profiles()
        except Exception as exc:
            messagebox.showerror("Profile not saved", str(exc))
            write(f"Profile not saved: {exc}")

    def start_monitor():
        try:
            config = load_config()
            usable = {name: prof for name, prof in config.get("apps", {}).items()
                      if isinstance(prof, dict) and isinstance(prof.get("slots"), dict) and prof.get("slots")}
            if not usable:
                raise RuntimeError("Save at least one placement profile before starting the monitor.")
            pid, argv = monitor_pid_and_argv()
            if pid is not None:
                # The new monitor has exactly: interpreter, script, --monitor.
                if len(argv) == 3 and argv[-1] == "--monitor":
                    write("Background monitor is already running. It reloads saved profiles automatically.")
                    return
                stop_managed_monitor()  # replace a legacy one-profile monitor
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    [sys.executable, str(SCRIPT_PATH), "--monitor"],
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            PID_FILE.write_text(str(proc.pid))
            write(f"Started multi-task monitor (PID {proc.pid}) for {len(usable)} configured executable(s).")
            write(f"Monitor log: {LOG_FILE}")
            write("The monitor continues after you close this window and reloads saved profiles every two seconds.")
        except Exception as exc:
            messagebox.showerror("Could not start monitor", str(exc))
            write(f"Could not start monitor: {exc}")

    def stop_monitor():
        write(stop_managed_monitor())
        write("Stopping the monitor does not restore existing CPU affinities.")

    def check_status():
        try:
            write(monitor_status())
            tasks = find_tasks()
            if not tasks:
                write("No PrimeGrid tasks detected.")
            for task in tasks:
                write(f"{task.name}: PID {task.pid}; {len(task.tids)} Linux thread(s); allowed CPUs {current_cpu_text(task)}.")
            config = load_config()
            for app_name, profile in sorted(config.get("apps", {}).items()):
                slots = profile.get("slots", {}) if isinstance(profile, dict) else {}
                for slot_id, settings in sorted(slots.items(), key=lambda pair: slot_sort_key(pair[0])):
                    write(f"Saved profile: {app_name}, slot {slot_id}, CCD/L3 {int(settings['ccd_index']) + 1}, {int(settings['core_count'])} core(s).")
        except Exception as exc:
            write(f"Status check failed: {exc}")

    def restore_selected():
        try:
            task = selected_task()
            if monitor_status().startswith("Background monitor: running"):
                answer = messagebox.askyesno(
                    "Stop monitoring?",
                    "Restoring this task while the monitor is running would be temporary. Stop the monitor first and restore this task?",
                )
                if not answer:
                    return
                write(stop_managed_monitor())
            actual, ppid, allowed = restore_parent_affinity(task.identity)
            write(f"Restored {actual.name} PID {actual.pid} to parent PID {ppid}'s allowed CPUs: {fmt_cpus(allowed)}.")
            refresh_tasks()
        except Exception as exc:
            messagebox.showerror("Could not restore affinity", str(exc))
            write(f"Restore failed: {exc}")

    ttk.Button(actions, text="Save & apply selected task", style="Primary.TButton",
               command=lambda: apply_selected(save=True)).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=3)
    ttk.Button(actions, text="Save profile only", style="TButton",
               command=save_profile).grid(row=0, column=1, sticky="ew", padx=3, pady=3)
    ttk.Button(actions, text="Start / update monitor", style="Success.TButton",
               command=start_monitor).grid(row=0, column=2, sticky="ew", padx=(6, 0), pady=3)
    ttk.Button(actions, text="Check status", style="TButton",
               command=check_status).grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=3)
    ttk.Button(actions, text="Stop monitor", style="Danger.TButton",
               command=stop_monitor).grid(row=1, column=1, sticky="ew", padx=3, pady=3)
    ttk.Button(actions, text="Restore selected task", style="TButton",
               command=restore_selected).grid(row=1, column=2, sticky="ew", padx=(6, 0), pady=3)

    ttk.Label(
        outer,
        text=("Core limit controls where a task can run; it does not change BOINC's calculation-thread count. "
              "Saved slots are reused automatically by the background monitor."),
        style="Muted.TLabel",
        wraplength=1080,
        font=("Sans", 9),
    ).pack(anchor="w", pady=(8, 0))

    refresh_topology()
    refresh_tasks(keep_selection=False)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--monitor":
        try:
            monitor_loop()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                    PID_FILE.unlink()
            except OSError:
                pass
    else:
        run_gui()
