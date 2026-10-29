#!/usr/bin/python3
"""Restricted privileged helper for PrimeGrid Affinity.

This helper is installed root-owned and invoked only through a per-user sudoers
rule. It can set affinity only for an executable whose resolved path is inside
BOINC's PrimeGrid project directory. It accepts no commands or arbitrary paths.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_MARKER = "/projects/www.primegrid.com/"
PROC = Path("/proc")
SYS_ONLINE = Path("/sys/devices/system/cpu/online")


def parse_cpu_list(value: str) -> set[int]:
    if not value or len(value) > 4096 or not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise ValueError("Invalid CPU list.")
    cpus: set[int] = set()
    for part in value.split(","):
        fields = [int(x) for x in part.split("-")]
        start, end = fields[0], fields[-1]
        if end < start or end - start > 65536:
            raise ValueError("Invalid CPU range.")
        cpus.update(range(start, end + 1))
    if not cpus:
        raise ValueError("The CPU list is empty.")
    return cpus


def read_cpu_list_file(value: str) -> set[int]:
    return parse_cpu_list(value.strip())


def process_start_ticks(pid: int) -> int:
    raw = (PROC / str(pid) / "stat").read_text(encoding="ascii")
    fields = raw.rsplit(")", 1)[1].split()
    # fields[0] is stat field 3; starttime is stat field 22 (index 19).
    return int(fields[19])


def verify_process(pid: int, expected_start: int) -> str:
    if pid <= 1 or expected_start <= 0:
        raise ValueError("Invalid process identity.")
    if process_start_ticks(pid) != expected_start:
        raise RuntimeError("The selected PrimeGrid task ended or its PID was reused. Refresh the task list.")
    exe_link = os.readlink(PROC / str(pid) / "exe")
    if exe_link.endswith(" (deleted)"):
        raise RuntimeError("The PrimeGrid executable has been deleted or replaced; no affinity change made.")
    exe_path = os.path.realpath(str(PROC / str(pid) / "exe"))
    if PROJECT_MARKER not in exe_path or not os.path.isfile(exe_path):
        raise PermissionError("Refusing to change affinity: target is not a live executable in BOINC's PrimeGrid project directory.")
    return exe_path


def thread_ids(pid: int) -> list[int]:
    root = PROC / str(pid) / "task"
    return sorted(int(entry.name) for entry in root.iterdir() if entry.name.isdigit())


def verify_tgid(tid: int, pid: int) -> None:
    status = (PROC / str(tid) / "status").read_text(encoding="ascii", errors="replace")
    for line in status.splitlines():
        if line.startswith("Tgid:"):
            if int(line.split()[1]) != pid:
                raise RuntimeError(f"TID {tid} no longer belongs to PID {pid}.")
            return
    raise RuntimeError(f"Could not validate thread {tid}.")


def apply(pid: int, expected_start: int, cpus: set[int]) -> None:
    online = read_cpu_list_file(SYS_ONLINE.read_text(encoding="ascii"))
    if not cpus <= online:
        raise ValueError("The requested CPU list contains offline or nonexistent CPUs.")

    old_masks: dict[int, set[int]] = {}
    touched: set[int] = set()
    try:
        verify_process(pid, expected_start)
        for _ in range(4):
            verify_process(pid, expected_start)
            tids = thread_ids(pid)
            if not tids:
                raise RuntimeError("The PrimeGrid task has no live threads.")
            for tid in tids:
                verify_tgid(tid, pid)
                if tid not in old_masks:
                    old_masks[tid] = os.sched_getaffinity(tid)
                os.sched_setaffinity(tid, cpus)
                touched.add(tid)
            verify_process(pid, expected_start)
            current_tids = thread_ids(pid)
            if set(current_tids) == set(tids):
                mismatches = [tid for tid in current_tids if os.sched_getaffinity(tid) != cpus]
                if mismatches:
                    raise RuntimeError(
                        "Linux did not apply the exact CPU mask to every thread (possibly due to a cpuset restriction); "
                        "no lasting change intended."
                    )
                print(f"Affinity applied to {len(current_tids)} PrimeGrid thread(s).")
                return
        raise RuntimeError("The task's thread list kept changing; no affinity change made.")
    except Exception as exc:
        # Restore every thread we modified before reporting a failure.
        for tid in sorted(touched):
            original = old_masks.get(tid)
            if original:
                try:
                    os.sched_setaffinity(tid, original)
                except (OSError, ProcessLookupError):
                    pass
        raise RuntimeError(f"Affinity helper failed; rollback attempted: {exc}") from exc


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "apply":
        print("Usage: affinity-helper.py apply PID START_TICKS CPU_LIST", file=sys.stderr)
        return 2
    try:
        pid = int(sys.argv[2], 10)
        start_ticks = int(sys.argv[3], 10)
        cpus = parse_cpu_list(sys.argv[4])
        apply(pid, start_ticks, cpus)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
