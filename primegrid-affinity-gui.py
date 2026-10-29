#!/usr/bin/env python3
"""PrimeGrid multi-task CPU affinity GUI for Linux/Zorin.

Profiles are saved per PrimeGrid executable and placement slot. Multiple live
instances can each select exact physical cores, with overlap warnings across
all saved profiles. A background monitor follows configured tasks, remembering the
slot assigned to each live process identity and freeing it when that process exits.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import signal
import stat
import socket
import xml.etree.ElementTree as ET
import re

APP_NAME = "PrimeGrid Affinity"
SYS = Path("/sys/devices/system/cpu")
PROC = Path("/proc")
STATE_DIR = Path.home() / ".local/state/primegrid-affinity-gui"
PID_FILE = STATE_DIR / "monitor.pid"
LOG_FILE = STATE_DIR / "monitor.log"
CONFIG_FILE = STATE_DIR / "settings.json"
LOCK_FILE = STATE_DIR / "settings.lock"
MONITOR_LOCK_FILE = STATE_DIR / "monitor.lock"
MONITOR_SERVICE_NAME = "primegrid-affinity-monitor.service"
MONITOR_SERVICE_FILE = Path.home() / ".config/systemd/user" / MONITOR_SERVICE_NAME
MONITOR_AUTOSTART_FILE = Path.home() / ".config/autostart/primegrid-affinity-monitor.desktop"
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_MARKER = "projects/www.primegrid.com/"
# BOINC plan classes and resource metadata identify GPU-backed variants. Keep this
# deliberately focused on backend/resource markers, not general project names.
GPU_RESOURCE_RE = re.compile(
    r"cuda|opencl|hip|rocm|vulkan|metal|nvidia|amdgpu|coproc_ati|coproc_amd|ati\d|gpu",
    re.IGNORECASE,
)
TASK_DISCOVERY_NOTE = ""
BOINC_RPC_HOST = "127.0.0.1"
BOINC_RPC_PORT = 31416
BOINC_RPC_TIMEOUT = 0.5
PRIVILEGED_HELPER = Path("/usr/local/libexec/primegrid-affinity-helper.py")
SUDO_PATH = Path("/usr/bin/sudo")
PYTHON3_PATH = Path("/usr/bin/python3")
MAX_SLOTS = 8
CONFIG_VERSION = 6


@dataclass
class BoincResult:
    name: str
    pid: int
    slot: int | None
    project_url: str = ""
    plan_class: str = ""
    resources: str = ""

    @property
    def uses_gpu(self) -> bool:
        """Return true when BOINC identifies this result as GPU-backed."""
        return bool(GPU_RESOURCE_RE.search(f"{self.plan_class} {self.resources}"))


@dataclass
class Task:
    pid: int
    name: str
    command: str
    tids: list[int]
    ppid: int
    start_ticks: int
    work_slot: int | None = None
    boinc_task_name: str | None = None
    boinc_pid: int | None = None
    boinc_slot: int | None = None
    boinc_uses_gpu: bool | None = None

    @property
    def identity(self) -> str:
        return f"{self.pid}:{self.start_ticks}"

    @property
    def display_name(self) -> str:
        return self.boinc_task_name or f"{self.name} (BOINC task unmatched)"

    @property
    def label(self) -> str:
        boinc_pid = f" — BOINC PID {self.boinc_pid}" if self.boinc_pid else ""
        return f"{self.display_name} — Worker PID {self.pid}{boinc_pid} — {len(self.tids)} process threads"


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


def _domain_topology_payload(domain_record):
    """Return a stable, JSON-safe representation of one L3-sharing domain."""
    domain_cpus, cores = domain_record
    ordered_cores = sorted(
        (tuple(sorted(int(cpu) for cpu in siblings)) for siblings in cores),
        key=lambda siblings: (min(siblings), siblings),
    )
    return {
        "cpus": sorted(int(cpu) for cpu in domain_cpus),
        "cores": [list(siblings) for siblings in ordered_cores],
    }


def _fingerprint(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def topology_fingerprint(domains) -> str:
    """Fingerprint all detected L3 domains and their physical-core siblings."""
    return _fingerprint([_domain_topology_payload(domain) for domain in domains])


def domain_fingerprint(domains, ccd_index: int) -> str:
    """Fingerprint the specific L3 domain selected by a profile."""
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    return _fingerprint(_domain_topology_payload(domains[ccd_index]))


def profile_topology_status(settings, domains) -> str:
    """Return current, unverified, or changed for a saved slot's topology stamp."""
    if not isinstance(settings, dict):
        return "unverified"
    saved_topology = settings.get("topology_signature")
    saved_domain = settings.get("domain_signature")
    if not saved_topology or not saved_domain:
        # Profiles made by earlier versions have no baseline to compare against.
        return "unverified"
    try:
        ccd_index = int(settings["ccd_index"])
        if ccd_index < 0 or ccd_index >= len(domains):
            return "changed"
        if saved_topology != topology_fingerprint(domains):
            return "changed"
        if saved_domain != domain_fingerprint(domains, ccd_index):
            return "changed"
        stored_cpus = settings.get("domain_cpu_list")
        if stored_cpus is not None and sorted(int(cpu) for cpu in stored_cpus) != sorted(domains[ccd_index][0]):
            return "changed"
    except (KeyError, TypeError, ValueError, RuntimeError):
        return "changed"
    return "current"


def topology_profile_metadata(domains, ccd_index: int) -> dict:
    """Metadata saved with every profile so automatic application can be checked."""
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    return {
        "topology_signature": topology_fingerprint(domains),
        "domain_signature": domain_fingerprint(domains, ccd_index),
        "domain_cpu_list": sorted(int(cpu) for cpu in domains[ccd_index][0]),
    }


def normalize_core_indices(selection, domain_core_count: int, core_start: int = 0) -> list[int]:
    """Return a validated, sorted list of zero-based physical-core indices.

    An integer selection is accepted for legacy profiles and means a contiguous
    count beginning at core_start. Lists/tuples/sets mean explicit core indices.
    """
    if domain_core_count < 1:
        raise RuntimeError("The selected CCD/L3 domain has no physical cores.")
    if isinstance(selection, int) and not isinstance(selection, bool):
        count = int(selection)
        start = int(core_start)
        if count < 1:
            raise RuntimeError("Choose at least 1 physical core.")
        if start < 0 or start >= domain_core_count:
            raise RuntimeError(f"Physical core index must be between 1 and {domain_core_count}.")
        indices = list(range(start, start + count))
    else:
        if not isinstance(selection, (list, tuple, set)):
            raise RuntimeError("The physical-core selection is invalid.")
        try:
            indices = [int(item) for item in selection]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("The physical-core selection contains an invalid index.") from exc
        if len(indices) != len(set(indices)):
            raise RuntimeError("The physical-core selection contains duplicate cores.")
        indices.sort()
    if not indices:
        raise RuntimeError("Select at least one physical core.")
    if any(index < 0 or index >= domain_core_count for index in indices):
        raise RuntimeError(
            f"The selection includes a core outside this CCD/L3 domain, which has "
            f"{domain_core_count} physical cores."
        )
    return indices


def core_indices_from_settings(settings, domain_core_count: int) -> list[int]:
    """Read explicit-core profiles and migrate older contiguous profiles in memory."""
    if "core_indices" in settings:
        selection = settings["core_indices"]
        return normalize_core_indices(selection, domain_core_count)
    count = int(settings.get("core_count", domain_core_count))
    start = int(settings.get("core_start", 0))
    return normalize_core_indices(count, domain_core_count, start)


def format_core_indices(indices) -> str:
    """Format zero-based physical-core indices as a compact 1-based list."""
    values = sorted(set(int(index) + 1 for index in indices))
    if not values:
        return "(none)"
    parts = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def targets_for(domains, ccd_index: int, core_selection, core_start: int = 0) -> set[int]:
    """Map selected physical-core indices to one logical CPU per physical core.

    For backward compatibility, an integer core_selection means a contiguous
    core count beginning at core_start. New profiles pass explicit index lists.
    SMT siblings are intentionally not included.
    """
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    cores = domains[ccd_index][1]
    indices = normalize_core_indices(core_selection, len(cores), core_start)
    return {cores[index][0] for index in indices}


def saved_profile_overlaps(config, domains):
    """Return pairs of configured profile slots whose allowed CPU sets overlap."""
    pools = []
    for app_name, profile in sorted(config.get("apps", {}).items()):
        if not isinstance(profile, dict):
            continue
        slots = profile.get("slots", {})
        if not isinstance(slots, dict):
            continue
        for slot_id, settings in sorted(slots.items(), key=lambda pair: slot_sort_key(pair[0])):
            if not isinstance(settings, dict):
                continue
            try:
                ccd_index = int(settings["ccd_index"])
                indices = core_indices_from_settings(settings, len(domains[ccd_index][1]))
                cpus = targets_for(domains, ccd_index, indices)
            except (IndexError, KeyError, TypeError, ValueError, RuntimeError):
                # Invalid/stale profiles are handled by normal profile validation.
                continue
            pools.append((str(app_name), str(slot_id), ccd_index, indices, cpus))

    overlaps = []
    for i, left in enumerate(pools):
        for right in pools[i + 1:]:
            shared = left[4] & right[4]
            if shared:
                overlaps.append({"left": left, "right": right, "cpus": shared})
    return overlaps


def profile_overlaps(config, domains, app_name, slot_id, ccd_index, core_selection, legacy_core_count=None):
    """Find saved slots whose CPU pool intersects the proposed selection.

    legacy_core_count supports callers using the prior (core_start, core_count)
    signature. New callers pass a list of selected zero-based physical-core indices.
    """
    if legacy_core_count is not None:
        proposed = targets_for(domains, ccd_index, legacy_core_count, int(core_selection))
    else:
        proposed = targets_for(domains, ccd_index, core_selection)
    conflicts = []
    for other_app, profile in sorted(config.get("apps", {}).items()):
        if not isinstance(profile, dict):
            continue
        slots = profile.get("slots", {})
        if not isinstance(slots, dict):
            continue
        for other_slot, settings in sorted(slots.items(), key=lambda pair: slot_sort_key(pair[0])):
            if str(other_app) == str(app_name) and str(other_slot) == str(slot_id):
                continue
            if not isinstance(settings, dict):
                continue
            try:
                other_ccd = int(settings["ccd_index"])
                other_indices = core_indices_from_settings(settings, len(domains[other_ccd][1]))
                other_cpus = targets_for(domains, other_ccd, other_indices)
            except (IndexError, KeyError, TypeError, ValueError, RuntimeError):
                continue
            shared = proposed & other_cpus
            if shared:
                conflicts.append({"app": str(other_app), "slot": str(other_slot), "cpus": shared})
    return conflicts


def _xml_node_text_and_tags(node: ET.Element | None) -> str:
    """Flatten resource metadata, including nested XML tag names, for classification."""
    if node is None:
        return ""
    parts: list[str] = []
    for child in node.iter():
        parts.append(child.tag)
        if child.text and child.text.strip():
            parts.append(child.text.strip())
    return " ".join(parts)


def parse_boinc_results_xml(xml_payload: bytes | str) -> list[BoincResult]:
    """Parse active PrimeGrid results, retaining plan-class/resource metadata."""
    root = ET.fromstring(xml_payload)
    results_node = root.find(".//results")
    if results_node is None:
        return []
    results: list[BoincResult] = []
    for result in results_node.findall("result"):
        active = result.find("active_task")
        if active is None:
            continue
        try:
            pid = int((active.findtext("pid") or "0").strip())
        except ValueError:
            continue
        if pid <= 0:
            continue
        name = (result.findtext("name") or "").strip()
        project_url = (result.findtext("project_url") or "").strip()
        # This application discovers workers from the PrimeGrid project tree.
        # Do not match a different project's result by slot number alone.
        if "primegrid.com" not in project_url.lower():
            continue
        try:
            slot_value = (active.findtext("slot") or "").strip()
            slot = int(slot_value) if slot_value else None
        except ValueError:
            slot = None
        plan_class = (result.findtext("plan_class") or "").strip()
        resources = _xml_node_text_and_tags(result.find("resources"))
        if name:
            results.append(BoincResult(
                name=name, pid=pid, slot=slot, project_url=project_url,
                plan_class=plan_class, resources=resources,
            ))
    return results


def get_boinc_active_results() -> list[BoincResult] | None:
    """Query the local BOINC client; return None if local GUI RPC is unavailable.

    get_results is a read-only GUI RPC that does not require the GUI RPC password
    for a local connection. The query is restricted to localhost and has a short
    timeout so a stopped client never blocks the GUI or monitor for long.
    """
    request = (
        "<boinc_gui_rpc_request><get_results><active_only>1</active_only>"
        "</get_results></boinc_gui_rpc_request>"
    ).encode("utf-8") + b"\x03"
    try:
        with socket.create_connection((BOINC_RPC_HOST, BOINC_RPC_PORT), timeout=BOINC_RPC_TIMEOUT) as sock:
            sock.settimeout(BOINC_RPC_TIMEOUT)
            sock.sendall(request)
            reply = bytearray()
            while b"\x03" not in reply:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                reply.extend(chunk)
                if len(reply) > 4 * 1024 * 1024:
                    return None
            payload = bytes(reply).split(b"\x03", 1)[0].strip()
            return parse_boinc_results_xml(payload)
    except (OSError, ET.ParseError, ValueError, TimeoutError):
        return None


def _pid_is_ancestor(ancestor_pid: int, descendant_pid: int) -> bool:
    """Whether ancestor_pid appears in descendant_pid's live parent chain."""
    current = descendant_pid
    seen = set()
    while current > 1 and current not in seen:
        seen.add(current)
        try:
            parent = read_ppid(current)
        except (OSError, ValueError, ProcessLookupError):
            return False
        if parent == ancestor_pid:
            return True
        current = parent
    return False


def _pids_related(first_pid: int, second_pid: int) -> bool:
    return (first_pid == second_pid or
            _pid_is_ancestor(first_pid, second_pid) or
            _pid_is_ancestor(second_pid, first_pid))


def match_boinc_results(tasks: list[Task], results: list[BoincResult], related=None) -> None:
    """Attach BOINC names/PIDs to worker tasks without making ambiguous guesses.

    Matching order: exact PID, unique BOINC slot/cwd-slot, then unique process
    ancestry. Unmatched tasks remain visible with their executable name as a
    fallback; each BOINC result and worker can be assigned at most once.
    """
    for task in tasks:
        task.boinc_task_name = None
        task.boinc_pid = None
        task.boinc_slot = None
        task.boinc_uses_gpu = None
    if not tasks or not results:
        return
    related = related or _pids_related
    free_tasks = {i for i in range(len(tasks))}
    free_results = {i for i in range(len(results))}

    def assign(task_index: int, result_index: int) -> None:
        task = tasks[task_index]
        result = results[result_index]
        task.boinc_task_name = result.name
        task.boinc_pid = result.pid
        task.boinc_slot = result.slot
        task.boinc_uses_gpu = result.uses_gpu
        free_tasks.discard(task_index)
        free_results.discard(result_index)

    # Exact PID is the strongest association.
    for result_index in list(free_results):
        candidates = [i for i in free_tasks if tasks[i].pid == results[result_index].pid]
        if len(candidates) == 1:
            assign(candidates[0], result_index)

    # BOINC assigns active work to numbered slot directories. Use this only if
    # both sides have a unique matching slot; this helps when wrappers differ.
    changed = True
    while changed:
        changed = False
        pairs = []
        for ti in free_tasks:
            slot = tasks[ti].work_slot
            if slot is None:
                continue
            for ri in free_results:
                result = results[ri]
                if result.slot is not None and slot == result.slot:
                    pairs.append((ti, ri))
        task_counts = {ti: sum(1 for a, _ in pairs if a == ti) for ti, _ in pairs}
        result_counts = {ri: sum(1 for _, b in pairs if b == ri) for _, ri in pairs}
        unique_pairs = [(ti, ri) for ti, ri in pairs if task_counts[ti] == 1 and result_counts[ri] == 1]
        for ti, ri in unique_pairs:
            if ti in free_tasks and ri in free_results:
                assign(ti, ri)
                changed = True

    # Last resort: only match a process-tree relation when unambiguous both ways.
    changed = True
    while changed:
        changed = False
        pairs = [(ti, ri) for ti in free_tasks for ri in free_results
                 if related(tasks[ti].pid, results[ri].pid)]
        task_counts = {ti: sum(1 for a, _ in pairs if a == ti) for ti, _ in pairs}
        result_counts = {ri: sum(1 for _, b in pairs if b == ri) for _, ri in pairs}
        unique_pairs = [(ti, ri) for ti, ri in pairs if task_counts[ti] == 1 and result_counts[ri] == 1]
        for ti, ri in unique_pairs:
            if ti in free_tasks and ri in free_results:
                assign(ti, ri)
                changed = True


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
        work_slot = None
        try:
            cwd = os.readlink(PROC / str(pid) / "cwd")
            slot_match = re.search(r"/slots/(\d+)(?:/|$)", cwd)
            if slot_match:
                work_slot = int(slot_match.group(1))
        except (OSError, ValueError):
            pass
        return Task(pid, name, " ".join(argv), tids, ppid, start_ticks, work_slot=work_slot)
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


def cpu_tasks_only(tasks: list[Task]) -> list[Task]:
    """Return only workers matched to BOINC results classified as CPU tasks."""
    return [task for task in tasks if task.boinc_uses_gpu is False]


def find_tasks() -> list[Task]:
    """Find active PrimeGrid CPU workers only, using BOINC result metadata.

    GPU tasks and unclassified workers are intentionally omitted: without BOINC
    RPC metadata, process names alone are not a reliable way to distinguish CPU
    variants from GPU variants.
    """
    global TASK_DISCOVERY_NOTE
    boinc_results = get_boinc_active_results()
    if boinc_results is None:
        TASK_DISCOVERY_NOTE = (
            "BOINC GUI RPC is unavailable, so CPU/GPU task type cannot be confirmed. "
            "No workers are shown until the local BOINC client is reachable."
        )
        return []
    if not boinc_results:
        TASK_DISCOVERY_NOTE = "BOINC reports no active PrimeGrid results."
        return []

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
    leaves = sorted(leaves, key=lambda t: (t.name.lower(), t.start_ticks, t.pid))
    match_boinc_results(leaves, boinc_results)

    cpu_tasks = cpu_tasks_only(leaves)
    gpu_count = sum(task.boinc_uses_gpu is True for task in leaves)
    unclassified_count = sum(task.boinc_uses_gpu is None for task in leaves)
    notes = [f"CPU-only filter: showing {len(cpu_tasks)} CPU task(s); hiding {gpu_count} GPU task(s)."]
    if unclassified_count:
        notes.append(f"Hiding {unclassified_count} unmatched worker(s) because their CPU/GPU type could not be confirmed.")
    TASK_DISCOVERY_NOTE = " ".join(notes)
    return cpu_tasks


def find_task_by_identity(identity: str) -> Task:
    for task in find_tasks():
        if task.identity == identity:
            return task
    raise RuntimeError("The selected PrimeGrid task has ended or restarted. Refresh the task list.")


def task_runs_as_other_user(task: Task) -> bool:
    """Whether this GUI user lacks same-UID permission to change the task."""
    if os.geteuid() == 0:
        return False
    try:
        return (PROC / str(task.pid)).stat().st_uid != os.geteuid()
    except OSError as exc:
        raise RuntimeError(f"Could not determine the PrimeGrid task owner: {exc}") from exc


def apply_with_privileged_helper(task: Task, allowed: set[int]) -> None:
    """Use the optional root-owned, PrimeGrid-only helper for cross-user tasks."""
    if not PRIVILEGED_HELPER.is_file():
        raise RuntimeError(
            "BOINC runs under a different Linux user, so Linux denied the affinity change. "
            "Install the restricted helper from the extracted application folder with: "
            "sudo bash install-affinity-helper.sh. This keeps BOINC under its dedicated account."
        )
    try:
        helper_stat = PRIVILEGED_HELPER.stat()
        if helper_stat.st_uid != 0 or helper_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(
                f"Refusing to use {PRIVILEGED_HELPER}: it must be root-owned and not group/world-writable. "
                "Reinstall it with sudo bash install-affinity-helper.sh."
            )
        # Every directory in this fixed system path must also be root-owned and
        # non-writable by group/other, or a user could replace the helper path.
        for parent in (PRIVILEGED_HELPER.parent, PRIVILEGED_HELPER.parent.parent, Path("/usr")):
            parent_stat = parent.stat()
            if parent_stat.st_uid != 0 or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError(f"Refusing unsafe privileged-helper directory: {parent}")
    except OSError as exc:
        raise RuntimeError(f"Cannot safely inspect the privileged helper: {exc}") from exc
    if not SUDO_PATH.is_file() or not PYTHON3_PATH.is_file():
        raise RuntimeError("The privileged affinity helper requires /usr/bin/sudo and /usr/bin/python3.")
    command = [
        str(SUDO_PATH), "-n", str(PYTHON3_PATH), "-I", str(PRIVILEGED_HELPER),
        "apply", str(task.pid), str(task.start_ticks), fmt_cpus(allowed),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run the privileged affinity helper: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"helper exited with code {result.returncode}").strip()
        raise RuntimeError(f"Privileged affinity helper failed: {detail}")


def apply_task_affinity(identity: str, ccd_index: int, core_selection, core_start: int = 0, domains=None):
    domains = cpu_topology() if domains is None else domains
    task = find_task_by_identity(identity)
    tids = sorted(int(item.name) for item in (PROC / str(task.pid) / "task").iterdir() if item.name.isdigit())
    if not tids:
        raise RuntimeError("The task no longer has live threads. No changes made.")
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    requested_indices = normalize_core_indices(core_selection, len(domains[ccd_index][1]), core_start)
    # Avoid assigning more CPU slots than the task currently has process threads.
    # If the task later gains threads, the monitor can apply the complete profile.
    effective_indices = requested_indices[:len(tids)]
    allowed = targets_for(domains, ccd_index, effective_indices)

    if task_runs_as_other_user(task):
        # The helper performs its own identity checks, applies the mask to all
        # worker threads, verifies the result, and attempts rollback on failure.
        apply_with_privileged_helper(task, allowed)
        current = find_task_by_identity(identity)
        tids = sorted(int(item.name) for item in (PROC / str(current.pid) / "task").iterdir() if item.name.isdigit())
        for tid in tids:
            if os.sched_getaffinity(tid) != allowed:
                raise RuntimeError(f"Affinity changed after helper verification for TID {tid}; check other CPU-management services.")
        task = current
    else:
        old = {tid: os.sched_getaffinity(tid) for tid in tids}
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

    if task_runs_as_other_user(task):
        apply_with_privileged_helper(task, allowed)
        current = find_task_by_identity(identity)
        for tid in sorted(int(item.name) for item in (PROC / str(current.pid) / "task").iterdir() if item.name.isdigit()):
            if os.sched_getaffinity(tid) != allowed:
                raise RuntimeError(f"Affinity changed after helper verification for TID {tid}; check other CPU-management services.")
        return current, ppid, allowed

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


def save_assignment(
    task: Task, slot_id: str, ccd_index: int, core_indices: list[int],
    live_tasks: list[Task], domains=None,
):
    domains = cpu_topology() if domains is None else domains
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    live_identities = {t.identity for t in live_tasks if t.name == task.name}
    selected = normalize_core_indices(core_indices, len(domains[ccd_index][1]))
    topology_meta = topology_profile_metadata(domains, ccd_index)

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
        profile["slots"][str(slot_id)] = {
            "ccd_index": int(ccd_index),
            "core_indices": selected,
            "core_count": len(selected),
            **topology_meta,
        }
        assignments[task.identity] = str(slot_id)
        data["last_selection"] = {"app_name": task.name, "slot_id": str(slot_id)}
    update_config(mutate)


def save_profile_only(app_name: str, slot_id: str, ccd_index: int, core_indices: list[int], domains=None):
    domains = cpu_topology() if domains is None else domains
    if ccd_index < 0 or ccd_index >= len(domains):
        raise RuntimeError("The selected CCD/L3 domain no longer exists. No changes made.")
    selected = normalize_core_indices(core_indices, len(domains[ccd_index][1]))
    topology_meta = topology_profile_metadata(domains, ccd_index)

    def mutate(data):
        profile = app_profile(data, app_name)
        profile["slots"][str(slot_id)] = {
            "ccd_index": int(ccd_index),
            "core_indices": selected,
            "core_count": len(selected),
            **topology_meta,
        }
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
    # When installed, systemd owns the monitor lifecycle. Stop the unit rather
    # than killing its process behind systemd's back.
    if MONITOR_SERVICE_FILE.is_file():
        if not shutil.which("systemctl"):
            return "Could not stop the automatic monitor: systemctl was not found."
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, text=True, timeout=15,
            )
            result = subprocess.run(
                ["systemctl", "--user", "stop", MONITOR_SERVICE_NAME],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Could not stop the automatic monitor service: {exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl returned an error").strip()
            return f"Could not stop the automatic monitor service: {detail}"
        PID_FILE.unlink(missing_ok=True)
        return "Stopped the automatic background monitor for this session. It remains enabled for the next login."

    # Compatibility fallback for installations made before the user service.
    pid, argv = monitor_pid_and_argv()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        if MONITOR_AUTOSTART_FILE.is_file():
            return "No active monitor found; automatic startup remains enabled for your next login."
        return "No active GUI-managed monitor found; cleared stale PID file if present."
    try:
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        if MONITOR_AUTOSTART_FILE.is_file():
            return f"Stopped background monitor (PID {pid}); automatic startup remains enabled for your next login."
        return f"Stopped background monitor (PID {pid})."
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        if MONITOR_AUTOSTART_FILE.is_file():
            return "Background monitor was already stopped; automatic startup remains enabled for your next login."
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
    monitor_lock = MONITOR_LOCK_FILE.open("a+")
    try:
        fcntl.flock(monitor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_line("Another background monitor is already running; exiting to prevent duplicate monitoring.")
        monitor_lock.close()
        return
    PID_FILE.write_text(str(os.getpid()))
    log_line("Multi-task background monitor started.")
    last_status = {}
    logged_overlaps = set()
    while True:
        try:
            config = load_config()
            tasks = find_tasks()
            by_app = {}
            for task in tasks:
                by_app.setdefault(task.name, []).append(task)

            configured_apps = config.get("apps", {})
            domains = cpu_topology()
            detected_overlaps = set()
            for conflict in saved_profile_overlaps(config, domains):
                left = conflict["left"]
                right = conflict["right"]
                key = (left[0], left[1], right[0], right[1], tuple(sorted(conflict["cpus"])))
                detected_overlaps.add(key)
                if key not in logged_overlaps:
                    log_line(
                        f"CPU POOL OVERLAP: {left[0]} slot {left[1]} and {right[0]} slot {right[1]} "
                        f"both allow CPUs {fmt_cpus(conflict['cpus'])}. Review profiles to avoid contention."
                    )
            logged_overlaps = detected_overlaps

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
                        topology_state = profile_topology_status(settings, domains)
                        if topology_state != "current":
                            reason = (
                                "has no saved CPU-topology fingerprint"
                                if topology_state == "unverified"
                                else "was saved for a different CPU topology"
                            )
                            note = (
                                f"TOPOLOGY REVIEW REQUIRED: {app_name} slot {slot} {reason}. "
                                "Automatic affinity changes skipped. Open the GUI, verify the CCD/L3 and core checkboxes, "
                                "then save the profile to confirm the current topology."
                            )
                            if last_status.get(identity) != note:
                                log_line(note)
                            last_status[identity] = note
                            continue
                        ccd_index = int(settings["ccd_index"])
                        if ccd_index < 0 or ccd_index >= len(domains):
                            raise RuntimeError("The selected CCD/L3 domain no longer exists.")
                        core_indices = core_indices_from_settings(settings, len(domains[ccd_index][1]))
                        tids = sorted(int(x.name) for x in (PROC / str(task.pid) / "task").iterdir() if x.name.isdigit())
                        if not tids:
                            continue
                        targets_for(domains, ccd_index, core_indices)
                        effective_indices = core_indices[:len(tids)]
                        target = targets_for(domains, ccd_index, effective_indices)
                        needs_change = any(os.sched_getaffinity(tid) != target for tid in tids)
                        if needs_change:
                            current, applied_tids, allowed, _ = apply_task_affinity(identity, ccd_index, core_indices, domains=domains)
                            actual_indices = core_indices[:len(allowed)]
                            pid_detail = (f"worker PID {current.pid}, BOINC PID {current.boinc_pid}"
                                          if current.boinc_pid is not None and current.boinc_pid != current.pid
                                          else f"worker PID {current.pid}")
                            msg = (f"Applied {current.display_name} ({pid_detail}) to slot {slot}, CCD/L3 {ccd_index + 1}, "
                                   f"physical core(s) {format_core_indices(actual_indices)}; "
                                   f"allowed CPUs {fmt_cpus(allowed)}; {len(applied_tids)} process thread(s).")
                            if len(allowed) < len(core_indices):
                                msg += f" Profile requested {len(core_indices)} cores; capped to {len(allowed)} for this task's current thread count."
                            if last_status.get(identity) != msg:
                                log_line(msg)
                            last_status[identity] = msg
                        else:
                            last_status.pop(identity, None)
                    except (OSError, ProcessLookupError, RuntimeError, KeyError, ValueError) as exc:
                        note = f"No affinity change for {task.display_name} (worker PID {task.pid}): {exc}"
                        if last_status.get(identity) != note:
                            log_line(note)
                        last_status[identity] = note

                for task in app_tasks:
                    if task.identity not in new_assignments:
                        note = (f"{task.display_name} (worker PID {task.pid}) not managed: no free configured slot. "
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
    style.configure("Warning.TLabel", background=SURFACE, foreground=RED,
                    font=("Sans", 9, "bold"), padding=(2, 4))
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
    columns = ("boinc_task", "worker_pid", "boinc_pid", "threads", "allowed", "slot")
    tasks_tree = ttk.Treeview(task_frame, columns=columns, show="headings", height=4, selectmode="browse")
    for col, title, width, stretch in [
        ("boinc_task", "BOINC task / result", 310, True),
        ("worker_pid", "Worker PID", 85, False),
        ("boinc_pid", "BOINC PID", 85, False),
        ("threads", "Process threads", 115, False),
        ("allowed", "Allowed CPUs", 220, True),
        ("slot", "Saved slot", 80, False),
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

    core_selection_frame = ttk.LabelFrame(setting_frame, text="  PHYSICAL CORES  ", style="Card.TLabelframe", padding=8)
    core_selection_frame.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(8, 4))
    core_selection_frame.columnconfigure(0, weight=1)
    core_checks_container = ttk.Frame(core_selection_frame, style="Card.TFrame")
    core_checks_container.pack(fill="x", expand=True)
    core_controls = ttk.Frame(core_selection_frame, style="Card.TFrame")
    core_controls.pack(fill="x", pady=(5, 0))
    selection_summary_var = tk.StringVar(value="Select the physical cores this task may use.")
    ttk.Label(core_controls, textvariable=selection_summary_var, style="Card.TLabel", wraplength=850,
              justify="left").pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Button(core_controls, text="Select all", style="TButton",
               command=lambda: select_all_cores()).pack(side="left", padx=(0, 5))
    ttk.Button(core_controls, text="Clear", style="TButton",
               command=lambda: clear_core_selection()).pack(side="left")

    hint_frame = ttk.Frame(setting_frame, style="Hint.TFrame")
    hint_frame.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(4, 0))
    profile_hint = ttk.Label(hint_frame, text="Select a running task to configure its placement profile.",
                             style="Hint.TLabel", wraplength=1050, justify="left", anchor="w")
    profile_hint.pack(fill="x")
    overlap_var = tk.StringVar(value="CPU pool overlap check will appear here.")
    ttk.Label(setting_frame, textvariable=overlap_var, style="Warning.TLabel", wraplength=1080,
              justify="left", anchor="w").grid(row=3, column=0, columnspan=8, sticky="ew", pady=(4, 0))

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
    saved_columns = ("application", "slot", "ccd", "range", "topology", "assigned")
    saved_tree = ttk.Treeview(saved_frame, columns=saved_columns, show="headings", height=4, selectmode="browse")
    for col, title, width, stretch in [
        ("application", "Application", 300, True), ("slot", "Slot", 60, False),
        ("ccd", "CCD / L3 domain", 150, False), ("range", "Physical cores", 135, False),
        ("topology", "Topology", 115, False), ("assigned", "Assigned worker PID", 170, True),
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
    core_checkbox_vars: dict[int, tk.BooleanVar] = {}
    selected_app_name = None

    def checked_core_indices() -> list[int]:
        return sorted(index for index, variable in core_checkbox_vars.items() if variable.get())

    def selected_core_indices() -> list[int]:
        selected = checked_core_indices()
        if not selected:
            raise RuntimeError("Select at least one physical core.")
        return selected

    def on_core_selection_changed():
        selected = checked_core_indices()
        if not selected:
            selection_summary_var.set("No cores selected. Select at least one core before saving or applying.")
        else:
            try:
                ccd_index = selected_ccd_index()
                cpus = targets_for(domains, ccd_index, selected)
                selection_summary_var.set(
                    f"Selected physical cores: {format_core_indices(selected)}  |  Allowed logical CPUs: {fmt_cpus(cpus)}"
                )
            except (RuntimeError, ValueError):
                selection_summary_var.set(f"Selected physical cores: {format_core_indices(selected)}")
        refresh_overlap_warning()

    def rebuild_core_checkboxes(selected_indices=None, allow_empty=False):
        for child in core_checks_container.winfo_children():
            child.destroy()
        core_checkbox_vars.clear()
        if not domains:
            return
        try:
            ccd_index = selected_ccd_index()
        except RuntimeError:
            ccd_index = 0
        cores = domains[ccd_index][1]
        if selected_indices is None:
            selected = set(range(len(cores)))
        else:
            selected = {int(index) for index in selected_indices if 0 <= int(index) < len(cores)}
            if not selected and not allow_empty:
                selected = {0}
        for index, siblings in enumerate(cores):
            variable = tk.BooleanVar(value=index in selected)
            core_checkbox_vars[index] = variable
            # The app deliberately selects one logical CPU per physical core.
            label = f"Core {index + 1} (CPU {siblings[0]})"
            check = ttk.Checkbutton(core_checks_container, text=label, variable=variable,
                                    command=on_core_selection_changed)
            check.grid(row=index // 6, column=index % 6, sticky="w", padx=(2, 12), pady=3)
        on_core_selection_changed()

    def select_all_cores():
        for variable in core_checkbox_vars.values():
            variable.set(True)
        on_core_selection_changed()

    def clear_core_selection():
        for variable in core_checkbox_vars.values():
            variable.set(False)
        on_core_selection_changed()

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

    def refresh_topology():
        nonlocal domains, ccd_labels
        previous_core_selection = checked_core_indices()
        domains = cpu_topology()
        ccd_labels = [f"CCD/L3 {i + 1} ({len(cores)} physical cores; CPUs {fmt_cpus(domain)})"
                      for i, (domain, cores) in enumerate(domains)]
        previous = ccd_var.get()
        ccd_combo.configure(values=ccd_labels)
        if previous not in ccd_labels:
            ccd_var.set(ccd_labels[0])
        rebuild_core_checkboxes(previous_core_selection or None)

    def ensure_gui_topology_current():
        """Block manual changes if CPU topology changed since this window opened."""
        fresh_domains = cpu_topology()
        if topology_fingerprint(fresh_domains) != topology_fingerprint(domains):
            raise RuntimeError(
                "The CPU topology changed after PrimeGrid Affinity opened. Close and reopen the GUI "
                "so CCD/L3 and physical-core checkboxes are rebuilt before saving or applying a profile."
            )
        return domains

    def current_profile_conflicts():
        ensure_gui_topology_current()
        task = selected_task()
        ccd_index = selected_ccd_index()
        core_indices = selected_core_indices()
        targets_for(domains, ccd_index, core_indices)
        config = load_config()
        return profile_overlaps(config, domains, task.name, str(slot_var.get()),
                                ccd_index, core_indices)

    def refresh_overlap_warning():
        try:
            conflicts = current_profile_conflicts()
            if not conflicts:
                overlap_var.set("No overlap with other saved CPU pools.")
                return
            parts = []
            for conflict in conflicts:
                parts.append(f"{conflict['app']} slot {conflict['slot']} (CPUs {fmt_cpus(conflict['cpus'])})")
            overlap_var.set("OVERLAP: This profile shares CPUs with " + "; ".join(parts) +
                            ". Choose another range to avoid contention, or confirm sharing when saving.")
        except (RuntimeError, ValueError, tk.TclError) as exc:
            overlap_var.set(f"Overlap check unavailable: {exc}")

    def confirm_profile_conflicts():
        conflicts = current_profile_conflicts()
        if not conflicts:
            return True
        lines = []
        for conflict in conflicts:
            lines.append(f"• {conflict['app']} slot {conflict['slot']}: CPUs {fmt_cpus(conflict['cpus'])}")
        return messagebox.askyesno(
            "Overlapping CPU pools",
            "This profile overlaps with existing saved profiles:\n\n" + "\n".join(lines) +
            "\n\nThe tasks will be allowed to compete for these CPUs.\n\nSave and continue anyway?"
        )

    def confirm_topology_review(task_name, slot_id, current_domains):
        config = load_config()
        profile = config.get("apps", {}).get(task_name, {})
        settings = profile.get("slots", {}).get(str(slot_id), {}) if isinstance(profile, dict) else {}
        if not settings:
            return True
        status = profile_topology_status(settings, current_domains)
        if status == "current":
            return True
        reason = (
            "This older profile has no saved CPU-topology baseline."
            if status == "unverified"
            else "The saved CPU topology differs from the current hardware/kernel topology."
        )
        return messagebox.askyesno(
            "Review CPU topology",
            f"{reason}\n\nAutomatic monitoring will skip this profile until it is reviewed. "
            "Check the selected CCD/L3 and every physical-core checkbox.\n\n"
            "Save this current selection as the new topology baseline and continue?"
        )

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
                topology_state = profile_topology_status(settings, domains)
                selection_error = None
                try:
                    saved_indices = core_indices_from_settings(settings, len(domains[ccd_index][1]))
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    selection_error = str(exc)
                    # Keep any still-valid saved indices, but never silently select all cores.
                    if isinstance(settings.get("core_indices"), list):
                        saved_indices = sorted({
                            int(index) for index in settings["core_indices"]
                            if str(index).lstrip("-").isdigit() and 0 <= int(index) < len(domains[ccd_index][1])
                        })
                    else:
                        try:
                            start = max(0, int(settings.get("core_start", 0)))
                            count = max(0, int(settings.get("core_count", len(domains[ccd_index][1]))))
                            saved_indices = [index for index in range(start, start + count) if index < len(domains[ccd_index][1])]
                        except (TypeError, ValueError):
                            saved_indices = []
                rebuild_core_checkboxes(saved_indices, allow_empty=True)
                if topology_state == "current":
                    hint = (f"Saved profile: {task.name}, slot {slot_id}. Settings are stored locally. "
                            f"This task currently has {len(task.tids)} process thread(s).")
                else:
                    state_text = ("has no saved topology baseline" if topology_state == "unverified"
                                  else "does not match the current CPU topology")
                    hint = (f"TOPOLOGY REVIEW REQUIRED: this profile {state_text}. Verify the selected CCD/L3 "
                            "and each core checkbox, then use Save profile only or Save & apply. The background "
                            "monitor will not apply it until it is re-saved.")
                    if selection_error:
                        hint += f" Some saved core selections do not fit this topology: {selection_error}."
                profile_hint.configure(text=hint)
            else:
                try:
                    numeric = int(slot_id)
                except ValueError:
                    numeric = 1
                ccd_index = (numeric - 1) % max(1, len(domains))
                if domains:
                    ccd_var.set(ccd_labels[ccd_index])
                    rebuild_core_checkboxes(list(range(len(domains[ccd_index][1]))))
                profile_hint.configure(
                    text=(f"No saved profile for {task.name}, slot {slot_id}; defaulting slot {slot_id} "
                          f"to CCD/L3 {ccd_index + 1}. This task currently has {len(task.tids)} process thread(s).")
                )
            on_core_selection_changed()
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
                    except (KeyError, TypeError, ValueError):
                        ccd_index = -1
                    topology_state = profile_topology_status(settings, domains)
                    if 0 <= ccd_index < len(domains):
                        ccd_text = f"CCD/L3 {ccd_index + 1} ({fmt_cpus(domains[ccd_index][0])})"
                        try:
                            selected_indices = core_indices_from_settings(settings, len(domains[ccd_index][1]))
                            core_range = f"{format_core_indices(selected_indices)} ({len(selected_indices)})"
                        except (TypeError, ValueError, RuntimeError):
                            core_range = "Review selection"
                    else:
                        ccd_text = "CCD/L3 unavailable"
                        core_range = "Review selection"
                    topology_text = "Current" if topology_state == "current" else "Review required"
                    pids = [str(live_by_identity[identity].pid) for identity, assigned in assignments.items()
                            if str(assigned) == str(slot_id) and identity in live_by_identity and live_by_identity[identity].name == app_name]
                    saved_tree.insert(
                        "", "end", iid=f"{app_name}::{slot_id}",
                        values=(app_name, slot_id, ccd_text, core_range, topology_text, ", ".join(pids) if pids else "Available"),
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
                    values=(task.display_name, task.pid, task.boinc_pid if task.boinc_pid is not None else "—",
                            len(task.tids), current_cpu_text(task), saved_slot),
                    tags=("even" if row_index % 2 == 0 else "odd",),
                )
            if previous in tasks_by_identity:
                tasks_tree.selection_set(previous)
            elif tasks:
                tasks_tree.selection_set(tasks[0].identity)
            if tasks:
                matched = sum(1 for task in tasks if task.boinc_task_name)
                write(f"Detected {len(tasks)} active CPU PrimeGrid task(s); matched {matched} to BOINC task names. Select a row to configure it.")
                if matched < len(tasks):
                    write("Some CPU task names could not be matched; those rows show the executable name as a fallback.")
                if TASK_DISCOVERY_NOTE:
                    write(TASK_DISCOVERY_NOTE)
            else:
                write(TASK_DISCOVERY_NOTE or "No active CPU PrimeGrid tasks detected. Start CPU tasks in BOINC and refresh.")
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
        current = checked_core_indices()
        if domains:
            try:
                new_ccd = selected_ccd_index()
                valid = [index for index in current if index < len(domains[new_ccd][1])]
            except RuntimeError:
                valid = current
            rebuild_core_checkboxes(valid or [0])
        refresh_overlap_warning()

    def on_slot_change(_event=None):
        load_slot_settings()

    tasks_tree.bind("<<TreeviewSelect>>", on_task_selected)
    ccd_combo.bind("<<ComboboxSelected>>", on_ccd_change)
    slot_combo.bind("<<ComboboxSelected>>", on_slot_change)

    def apply_selected(save=True):
        try:
            current_domains = ensure_gui_topology_current()
            task = selected_task()
            slot_id = str(slot_var.get())
            ccd_index = selected_ccd_index()
            core_indices = selected_core_indices()
            targets_for(current_domains, ccd_index, core_indices)
            if save and not confirm_topology_review(task.name, slot_id, current_domains):
                write("Apply cancelled; topology review is required before this profile can be updated.")
                return
            if save and not confirm_profile_conflicts():
                write("Apply cancelled; resolve or confirm CPU-pool overlap first.")
                return
            live_tasks = find_tasks()
            if save:
                save_assignment(task, slot_id, ccd_index, core_indices, live_tasks, domains=current_domains)
            actual, tids, target_cpus, _ = apply_task_affinity(task.identity, ccd_index, core_indices, domains=current_domains)
            actual_indices = core_indices[:len(target_cpus)]
            pid_detail = (f"worker PID {actual.pid}, BOINC PID {actual.boinc_pid}"
                          if actual.boinc_pid is not None and actual.boinc_pid != actual.pid
                          else f"worker PID {actual.pid}")
            write(f"Applied {actual.display_name} ({pid_detail}) to slot {slot_id}, CCD/L3 {ccd_index + 1}.")
            write(f"Allowed CPUs: {fmt_cpus(target_cpus)}; physical cores {format_core_indices(actual_indices)}; across {len(tids)} process thread(s).")
            if len(actual_indices) < len(core_indices):
                write(f"Profile selected {len(core_indices)} cores; applied {len(actual_indices)} because the task currently has fewer process threads.")
            write("Each thread may run on any CPU in the selected pool; the calculation thread count is unchanged.")
            refresh_tasks()
            refresh_saved_profiles()
        except Exception as exc:
            messagebox.showerror("Affinity not applied", str(exc))
            write(f"No affinity change confirmed: {exc}")

    def save_profile():
        try:
            current_domains = ensure_gui_topology_current()
            task = selected_task()
            slot_id = str(slot_var.get())
            ccd_index = selected_ccd_index()
            core_indices = selected_core_indices()
            targets_for(current_domains, ccd_index, core_indices)
            if not confirm_topology_review(task.name, slot_id, current_domains):
                write("Profile save cancelled; topology review is required before this profile can be updated.")
                return
            if not confirm_profile_conflicts():
                write("Profile save cancelled; resolve or confirm CPU-pool overlap first.")
                return
            save_profile_only(task.name, slot_id, ccd_index, core_indices, domains=current_domains)
            write(f"Saved profile for {task.name}, slot {slot_id}, CCD/L3 {ccd_index + 1}, physical cores {format_core_indices(core_indices)}. No running task was changed.")
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
            if MONITOR_SERVICE_FILE.is_file():
                if not shutil.which("systemctl"):
                    raise RuntimeError("The automatic monitor service is installed, but systemctl was not found.")
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True, text=True, timeout=15,
                )
                result = subprocess.run(
                    ["systemctl", "--user", "start", MONITOR_SERVICE_NAME],
                    capture_output=True, text=True, timeout=20,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "systemctl returned an error").strip()
                    raise RuntimeError(f"Could not start the automatic monitor service: {detail}")
                write("Automatic background monitor service is active or has been started. It reloads saved profiles every two seconds.")
                write(f"Monitor log: {LOG_FILE}")
                return
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
                boinc_pid = f"; BOINC PID {task.boinc_pid}" if task.boinc_pid is not None else "; BOINC PID unmatched"
                write(f"{task.display_name}: worker PID {task.pid}{boinc_pid}; {len(task.tids)} process thread(s); allowed CPUs {current_cpu_text(task)}.")
            config = load_config()
            domains = cpu_topology()
            conflicts = saved_profile_overlaps(config, domains)
            if conflicts:
                write(f"WARNING: {len(conflicts)} saved CPU-pool overlap(s) detected:")
                for conflict in conflicts:
                    left, right = conflict["left"], conflict["right"]
                    write(f"Overlap: {left[0]} slot {left[1]} / {right[0]} slot {right[1]} both allow CPUs {fmt_cpus(conflict['cpus'])}.")
            else:
                write("No overlaps between saved CPU pools.")
            for app_name, profile in sorted(config.get("apps", {}).items()):
                slots = profile.get("slots", {}) if isinstance(profile, dict) else {}
                for slot_id, settings in sorted(slots.items(), key=lambda pair: slot_sort_key(pair[0])):
                    try:
                        ccd_index = int(settings["ccd_index"])
                        if ccd_index < 0 or ccd_index >= len(domains):
                            raise RuntimeError("CCD/L3 domain no longer exists")
                        indices = core_indices_from_settings(settings, len(domains[ccd_index][1]))
                        core_text = f"CCD/L3 {ccd_index + 1}, physical cores {format_core_indices(indices)} ({len(indices)} core(s))"
                    except (KeyError, TypeError, ValueError, RuntimeError):
                        core_text = "CPU selection requires review"
                    topology_state = profile_topology_status(settings, domains)
                    topology_text = "current" if topology_state == "current" else f"REVIEW REQUIRED ({topology_state})"
                    write(f"Saved profile: {app_name}, slot {slot_id}, {core_text}; topology: {topology_text}.")
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
        text=("Select the exact physical cores a task may use. Each selected physical core contributes one logical CPU; "
              "this does not change BOINC's calculation-thread count. Saved slots are reused by the background monitor."),
        style="Muted.TLabel",
        wraplength=1080,
        font=("Sans", 9),
    ).pack(anchor="w", pady=(8, 0))

    refresh_topology()
    refresh_tasks(keep_selection=False)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--monitor":
        def _request_monitor_shutdown(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _request_monitor_shutdown)
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
