# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Linux thread affinity and short-lived CPU or NUMA load processes."""

import ctypes
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


PROFILES = {"cpu-cores", "cpu-contend", "numa-remote", "numa-bandwidth"}
_CHILDREN = {}
_NODE_PAGES = re.compile(r"N(\d+)=(\d+)")
_PR_SET_PDEATHSIG = 1


def _positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _cpus(value):
    if not isinstance(value, list) or not value or any(type(cpu) is not int or cpu < 0 for cpu in value):
        raise ValueError("cpus must be a nonempty list of nonnegative CPU numbers")
    if len(set(value)) != len(value):
        raise ValueError("cpus contains a duplicate CPU number")
    return sorted(value)


def _stat(pid, tid=None):
    path = Path(f"/proc/{pid}")
    if tid is not None:
        path = path / "task" / str(tid)
    fields = (path / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    return fields[0], int(fields[19]), int(fields[11]) + int(fields[12])


def _matches(pid, start, tid=None):
    try:
        state, observed_start, _ = _stat(pid, tid)
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state not in {"Z", "X"} and observed_start == start


def _thread_masks(pid, *, strict=True):
    masks = []
    for task in sorted(Path(f"/proc/{pid}/task").iterdir(), key=lambda path: int(path.name)):
        tid = int(task.name)
        try:
            state, start, _ = _stat(pid, tid)
            if state in {"Z", "X"}:
                continue
            cpus = sorted(os.sched_getaffinity(tid))
        except (FileNotFoundError, ProcessLookupError):
            if strict:
                raise
            continue
        if not _matches(pid, start, tid):
            if strict:
                raise ValueError(f"thread {tid} changed during affinity inspection")
            continue
        masks.append({"tid": tid, "start": start, "before": cpus})
    return masks


def _cpus_by_node(cpus):
    nodes = {}
    for cpu in cpus:
        locations = list(Path(f"/sys/devices/system/cpu/cpu{cpu}").glob("node[0-9]*"))
        if len(locations) != 1:
            raise ValueError(f"NUMA node for CPU {cpu} is unavailable")
        node = int(locations[0].name[4:])
        nodes.setdefault(node, []).append(cpu)
    return nodes


def _node_for(cpus):
    nodes = _cpus_by_node(cpus)
    if len(nodes) != 1:
        raise ValueError("the selected CPU set must belong to one NUMA node")
    return next(iter(nodes))


def _effective_cpus(pid):
    # Import here because load helpers run this file directly.
    from .cgroup import resolve_path

    path, version = resolve_path(pid, "cpuset")
    names = ("cpuset.cpus.effective",) if version == 2 else ("cpuset.effective_cpus", "cpuset.cpus")
    device = path.stat().st_dev
    for directory in (path, *path.parents):
        if directory.stat().st_dev != device:
            break
        for name in names:
            try:
                ranges = (directory / name).read_text(encoding="ascii").strip()
            except FileNotFoundError:
                continue
            if ranges:
                cpus = set()
                for part in ranges.split(","):
                    first, _, last = part.partition("-")
                    cpus.update(range(int(first), int(last or first) + 1))
                return cpus
    raise ValueError(f"effective CPU set is unavailable for PID {pid}")


def _reduced_cpus(affinity, threads):
    masks = [set(row["before"]) for row in threads]
    keep = set(affinity)
    desired = max(1, len(affinity) // 2)
    for cpu in sorted(affinity, reverse=True):
        if len(keep) <= desired:
            break
        candidate = keep - {cpu}
        if all(mask & candidate for mask in masks):
            keep = candidate
    if keep == affinity:
        raise ValueError("cannot reduce CPU count while keeping at least one eligible CPU per thread")
    return sorted(keep)


def _anon_pages(pid):
    totals = {}
    with Path(f"/proc/{pid}/numa_maps").open(encoding="utf-8") as maps:
        for line in maps:
            if " anon=" not in line:
                continue
            for node, pages in _NODE_PAGES.findall(line):
                totals[node] = totals.get(node, 0) + int(pages)
    return totals


def _require_pages(pid, node):
    pages = _anon_pages(pid)
    if pages.get(str(node), 0) * 2 <= sum(pages.values()):
        raise ValueError(f"PID {pid} has no majority of anonymous pages on NUMA node {node}")
    return pages


def _require_helper_control():
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ValueError("resource load helpers require Linux pidfd support")
    os.close(os.pidfd_open(os.getpid()))


def prepare(profile, target):
    if profile not in PROFILES:
        raise ValueError(f"unknown resource profile {profile}")
    pid = _positive_int(target.get("pid"), "pid")
    if pid in {1, os.getpid(), os.getppid()}:
        raise ValueError("target must be a workload process")
    state, start, _ = _stat(pid)
    if state in {"Z", "X"}:
        raise ValueError(f"target PID {pid} has exited")
    threads = _thread_masks(pid)
    if not threads or not _matches(pid, start):
        raise ValueError(f"target PID {pid} changed during inspection")
    affinity = set().union(*(row["before"] for row in threads))
    step = {"profile": profile, "resource": f"pid:{pid}:{start}", "pid": pid, "start": start,
            "threads": threads, "helpers": []}

    if profile == "cpu-contend":
        cpu = target.get("cpu")
        eligible = os.sched_getaffinity(0).intersection(*(row["before"] for row in threads))
        if cpu is None:
            if not eligible:
                raise ValueError("the workload threads and the load helper have no common eligible CPU")
            cpu = min(eligible)
        if type(cpu) is not int or cpu not in eligible:
            raise ValueError("the selected CPU must be eligible for every target thread and the load helper")
        _require_helper_control()
        for row in threads:
            row["after"] = [cpu]
        step["helper_cpus"] = [cpu]
        return step

    configured = target.get("cpus")
    if profile == "cpu-cores":
        requested = _reduced_cpus(affinity, threads) if configured is None else _cpus(configured)
        if not set(requested) < affinity:
            raise ValueError("cpus must be a strict subset of the workload's original CPU set")
        for row in threads:
            row["after"] = sorted(set(row["before"]).intersection(requested))
            if not row["after"]:
                raise ValueError(f"the CPU restriction would exclude all CPUs of thread {row['tid']}")
        return step

    pages = _anon_pages(pid)
    if not pages:
        raise ValueError(f"PID {pid} has no anonymous NUMA page counts")
    home = int(max(pages, key=pages.get))
    if pages[str(home)] * 2 <= sum(pages.values()):
        raise ValueError(f"PID {pid} has no majority of anonymous pages on a single NUMA node")
    step.update(numa_home=home, numa_before=pages)
    if profile == "numa-remote":
        if home not in _cpus_by_node(affinity):
            raise ValueError("the workload is already restricted to CPUs outside its memory NUMA node")
        if configured is None:
            remote = _cpus_by_node(_effective_cpus(pid))
            remote.pop(home, None)
            if not remote:
                raise ValueError("the target cgroup has no eligible CPU on another NUMA node")
            node = min(remote, key=lambda item: (-len(remote[item]), item))
            requested = sorted(remote[node])[:len(affinity)]
        else:
            requested = _cpus(configured)
            node = _node_for(requested)
        if node == home:
            raise ValueError("numa-remote requires CPUs on a different NUMA node")
        step["numa_destination"] = node
        for row in threads:
            row["after"] = requested
    else:
        helper_affinity = os.sched_getaffinity(0)
        if configured is None:
            available = _cpus_by_node(helper_affinity - affinity).get(home, [])
            if not available:
                raise ValueError("no eligible helper CPU on the workload's memory NUMA node outside its affinity")
            requested = sorted(available)[:2]
        else:
            requested = _cpus(configured)
        if _node_for(requested) != home or affinity.intersection(requested):
            raise ValueError("helper CPUs must be on the workload's NUMA node and outside its affinity")
        if not set(requested) <= helper_affinity:
            raise ValueError("the selected helper CPUs are outside the injector's affinity")
        mib = _positive_int(target.get("mib", 128), "mib")
        if mib < 32 or mib * len(requested) > 512:
            raise ValueError("numa-bandwidth requires at least 32 MiB per helper and at most 512 MiB total")
        _require_helper_control()
        step.update(helper_cpus=requested, helper_mib=mib)
    return step


def _set_affinity(pid, row, cpus):
    tid = row["tid"]
    if not _matches(pid, row["start"], tid):
        raise ValueError(f"thread {tid} changed before setting CPU affinity")
    previous = sorted(os.sched_getaffinity(tid))
    os.sched_setaffinity(tid, cpus)
    if not _matches(pid, row["start"], tid):
        raise ValueError(f"thread {tid} exited while setting CPU affinity")
    if sorted(os.sched_getaffinity(tid)) != cpus:
        os.sched_setaffinity(tid, previous)
        if sorted(os.sched_getaffinity(tid)) != previous:
            raise ValueError(f"CPU affinity readback differed and thread {tid} did not accept its previous mask")
        raise ValueError(f"thread {tid} did not accept CPU affinity {cpus}")


def _start_helper(step, cpu):
    ready, writer = os.pipe()
    process = None
    try:
        args = [sys.executable, "-B", str(Path(__file__).resolve()), "_load", str(writer),
                str(os.getpid()), str(step["pid"]), str(step["start"]), str(cpu),
                str(step.get("helper_mib", 0)), str(step["seconds"]), str(step.get("numa_home", -1))]
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, pass_fds=(writer,), start_new_session=True)
        os.close(writer)
        writer = None
        readable, _, _ = select.select([ready], [], [], 8)
        payload = os.read(ready, 8192) if readable else b""
        message = json.loads(payload) if payload else {}
        if not message.get("ready"):
            raise ValueError(message.get("error", f"load helper on CPU {cpu} failed to start"))
        _, start, _ = _stat(process.pid)
        step["helpers"].append({"pid": process.pid, "start": start, "cpu": cpu,
                                "numa_pages": message.get("numa_pages", {})})
        _CHILDREN[process.pid] = process
    except Exception:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        if writer is not None:
            os.close(writer)
        os.close(ready)


def apply(step):
    pid = step["pid"]
    before = [{key: row[key] for key in ("tid", "start", "before")} for row in step["threads"]]
    if not _matches(pid, step["start"]) or _thread_masks(pid) != before:
        raise ValueError("workload process or its affinity changed after preparation")
    if "numa_home" in step:
        _require_pages(pid, step["numa_home"])
    try:
        for row in step["threads"]:
            if "after" in row and row["after"] != row["before"]:
                _set_affinity(pid, row, row["after"])
        for cpu in step.get("helper_cpus", []):
            _start_helper(step, cpu)
        if not _matches(pid, step["start"]):
            raise ValueError("workload exited while applying resource fault")
    except Exception:
        restore(step)
        raise
    return observe(step)


def _stop_helper(helper):
    pid = helper["pid"]
    process = _CHILDREN.get(pid)
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        fd = None
    if fd is not None:
        try:
            if _matches(pid, helper["start"]):
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        signal.pidfd_send_signal(fd, sig)
                    except ProcessLookupError:
                        break
                    if poll.poll(2000):
                        break
                else:
                    raise ValueError(f"load helper PID {pid} did not exit")
        finally:
            os.close(fd)
    if process is not None:
        process.poll()
        _CHILDREN.pop(pid, None)


def restore(step):
    errors = []
    for helper in step["helpers"]:
        try:
            _stop_helper(helper)
        except (OSError, ValueError) as error:
            errors.append(str(error))
    pid = step["pid"]
    if _matches(pid, step["start"]):
        for row in step["threads"]:
            if "after" not in row or not _matches(pid, row["start"], row["tid"]):
                continue
            try:
                current = sorted(os.sched_getaffinity(row["tid"]))
                if current == row["before"]:
                    continue
                if current != row["after"]:
                    raise ValueError(f"CPU affinity of thread {row['tid']} changed outside this session")
                _set_affinity(pid, row, row["before"])
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError) as error:
                errors.append(str(error))
        injected = [row["after"] for row in step["threads"] if row.get("after", row["before"]) != row["before"]]
        if injected:
            tracked = {(row["tid"], row["start"]) for row in step["threads"]}
            original = {tuple(row["before"]) for row in step["threads"]}
            try:
                untracked = [row for row in _thread_masks(pid, strict=False)
                             if (row["tid"], row["start"]) not in tracked and row["before"] in injected]
                if untracked:
                    if len(original) == 1:
                        inherited = list(next(iter(original)))
                        for row in untracked:
                            if _matches(pid, row["start"], row["tid"]) and sorted(os.sched_getaffinity(row["tid"])) == row["before"]:
                                _set_affinity(pid, row, inherited)
                    else:
                        errors.append(f"original affinity is unknown for new threads {[row['tid'] for row in untracked]}")
            except (FileNotFoundError, ProcessLookupError):
                pass
            except (OSError, ValueError) as error:
                errors.append(str(error))
    if errors:
        raise ValueError(" | ".join(errors))
    return observe(step)


def observe(step):
    pid = step["pid"]
    alive = _matches(pid, step["start"])
    status = {"pid": pid, "target_alive": alive, "helpers": []}
    if alive:
        try:
            tracked = {(row["tid"], row["start"]) for row in step["threads"]}
            threads = _thread_masks(pid, strict=False)
            status["thread_affinity"] = {str(row["tid"]): row["before"] for row in threads}
            status["new_threads"] = [row["tid"] for row in threads if (row["tid"], row["start"]) not in tracked]
            status["cpu_ticks"] = _stat(pid)[2]
            if "numa_home" in step:
                status["numa_home"] = step["numa_home"]
                status["numa_pages"] = _anon_pages(pid)
            if "numa_destination" in step:
                status["numa_destination"] = step["numa_destination"]
        except (FileNotFoundError, ProcessLookupError):
            status["target_alive"] = False
    for helper in step["helpers"]:
        running = _matches(helper["pid"], helper["start"])
        status["helpers"].append({**helper, "alive": running})
    return status


def _load(writer, parent, target_pid, target_start, cpu, mib, seconds, numa_home):
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
            raise ValueError("load helper could not bind its lifetime to the supervisor")
        os.sched_setaffinity(0, {cpu})
        pages = {}
        if mib:
            size = mib * 1024 * 1024 // 2
            source = bytearray(size)
            dest = bytearray(size)
            source_addr = ctypes.addressof(ctypes.c_char.from_buffer(source))
            dest_addr = ctypes.addressof(ctypes.c_char.from_buffer(dest))
            ctypes.memset(source_addr, 1, size)
            ctypes.memset(dest_addr, 2, size)
            pages = _require_pages(os.getpid(), numa_home)
        os.write(writer, json.dumps({"ready": True, "numa_pages": pages}).encode())
    except (OSError, ValueError, MemoryError) as error:
        os.write(writer, json.dumps({"error": str(error)}).encode())
        return
    finally:
        os.close(writer)

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _matches(target_pid, target_start):
        end = min(deadline, time.monotonic() + 0.1)
        if mib:
            while time.monotonic() < end:
                ctypes.memmove(dest_addr, source_addr, size)
        else:
            while time.monotonic() < end:
                pass


if __name__ == "__main__" and len(sys.argv) == 10 and sys.argv[1] == "_load":
    _load(*(int(value) for value in sys.argv[2:]))
