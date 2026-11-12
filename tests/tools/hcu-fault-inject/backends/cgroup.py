# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Apply and restore resource limits on the Linux cgroup of a selected process."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import re


CONTROLLERS = {
    "cpu-low": "cpu",
    "memory-reclaim": "memory",
    "memory-oom": "memory",
    "pids-low": "pids",
}


def _unescape_mount_field(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _mounts():
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields, _, filesystem = line.partition(" - ")
        fields = fields.split()
        root, mountpoint = map(_unescape_mount_field, fields[3:5])
        fstype, _, options = filesystem.split()
        yield Path(mountpoint), PurePosixPath(root), fstype, options.split(","), "rw" in fields[5].split(",")


def _mount_version(fstype, options, controller):
    if fstype == "cgroup2":
        return 2
    if fstype == "cgroup" and controller in options:
        return 1
    return None


def _mount_for(path, controller, mounts):
    # A deeper, possibly non-cgroup mount can hide part of a hierarchy.
    match = None
    for mount in mounts:
        if path.is_relative_to(mount[0]) and (match is None or len(mount[0].parts) >= len(match[0].parts)):
            match = mount
    if match is not None:
        mount, root, fstype, options, writable = match
        version = _mount_version(fstype, options, controller)
        if version is not None:
            return mount, root, version, writable
    raise ValueError(f"{path} is not on a {controller} cgroup hierarchy")


def _membership(pid, controller):
    unified = None
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        hierarchy, controllers, member = line.split(":", 2)
        if controller in controllers.split(","):
            version = 1
            break
        if hierarchy == "0" and not controllers:
            unified = member
    else:
        if unified is None:
            raise ValueError(f"cannot identify the {controller} cgroup of PID {pid}")
        version = 2
        member = unified
    path = PurePosixPath(member)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"PID {pid} is outside the visible {controller} cgroup namespace")
    return version, path


def _resolve(pid, controller, membership):
    version, member = membership
    mounts = list(_mounts())
    found = {}
    for mount, root, fstype, options, _ in mounts:
        if _mount_version(fstype, options, controller) != version:
            continue
        # The proc path can be relative to the cgroup namespace rather than the
        # mount's filesystem root. Confirm the mapping by reading cgroup.procs.
        relative_paths = {member.relative_to("/")}
        if member.is_relative_to(root):
            relative_paths.add(member.relative_to(root))
        for relative in relative_paths:
            try:
                path = (mount / relative).resolve(strict=True)
                actual_mount, actual_root, actual_version, writable = _mount_for(path, controller, mounts)
                if actual_mount != mount or actual_version != version:
                    continue
                with _directory(path) as directory:
                    if str(pid) not in _read(directory, "cgroup.procs").split():
                        continue
                    info = os.fstat(directory)
            except (OSError, ValueError):
                continue
            identity = (info.st_dev, info.st_ino)
            preference = (not writable, path == mount, str(path))
            if identity not in found or preference < found[identity][0]:
                found[identity] = (preference, path, mount, actual_root)
    if len(found) != 1:
        raise ValueError(f"cannot uniquely resolve the mounted {controller} cgroup for PID {pid}")
    _, path, mount, root = next(iter(found.values()))
    return path, mount, root


def resolve_path(pid, controller):
    """Return the visible cgroup path and hierarchy version for read-only lookups."""
    membership = _membership(pid, controller)
    path, _, _ = _resolve(pid, controller, membership)
    return path, membership[0]


def _guard_injector(path, mount, root, version, controller):
    relative = path.relative_to(mount)
    host_target = root.joinpath(*relative.parts)
    visible_target = PurePosixPath("/").joinpath(*relative.parts)
    own_version, own = _membership("self", controller)
    if own_version != version or own.is_relative_to(host_target) or own.is_relative_to(visible_target):
        raise ValueError(f"{path} may contain the injector or be its ancestor")


def _pid_owner(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[2])
    raise ValueError(f"cannot identify the effective owner of PID {pid}")


def _pid_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X", "x"}:
        raise ValueError(f"target PID {pid} has exited")
    return int(fields[19]), _pid_owner(pid)


def _guard_group(directory, pid, owner):
    if any(entry.is_dir(follow_symlinks=False) for entry in os.scandir(directory)):
        raise ValueError("the selected PID must be in a dedicated leaf cgroup")
    members = sorted({int(member) for member in _read(directory, "cgroup.procs").split()})
    if pid not in members or os.getpid() in members:
        raise ValueError("cgroup membership changed or includes the injector")
    for member in members:
        try:
            if _pid_owner(member) != owner:
                raise ValueError("the selected cgroup contains processes with different owners")
        except FileNotFoundError:
            if member == pid:
                raise
    return members


@contextmanager
def _directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _control(directory, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW, dir_fd=directory)
    try:
        if os.fstat(fd).st_dev != os.fstat(directory).st_dev:
            raise ValueError(f"{name} is not on the target cgroup filesystem")
        yield fd
    finally:
        os.close(fd)


def _read(directory, name):
    with _control(directory, name, os.O_RDONLY) as fd:
        # cgroup limit and statistic files are small, seekless kernel interfaces.
        chunks = iter(lambda: os.read(fd, 4096), b"")
        return b"".join(chunks).decode("ascii").strip()


def _write(directory, name, value):
    payload = (value + "\n").encode("ascii")
    with _control(directory, name, os.O_WRONLY | os.O_NONBLOCK) as fd:
        if os.write(fd, payload) != len(payload):
            raise OSError(f"short write to cgroup control {name}")


def _positive_int(value, label, minimum=1):
    if type(value) is not int or not minimum <= value < 2**63:
        raise ValueError(f"{label} must be an integer between {minimum} and {2**63 - 1}")
    return value


def _requested_setting(profile, value, directory, version, before):
    if profile == "cpu-low":
        if version == 2:
            old_quota, period_text = before.split()
        else:
            old_quota, period_text = before, _read(directory, "cpu.cfs_period_us")
        period = int(period_text)
        unlimited = old_quota in {"max", "-1"}
        if value is None:
            quota = max(1000, (period if unlimited else int(old_quota)) // 4)
        else:
            quota = period * _positive_int(value, "CPU quota percentage") // 100
        if not 1000 <= quota < 2**63:
            raise ValueError("the requested CPU quota is outside the cgroup controller range")
        if not unlimited and quota >= int(old_quota):
            raise ValueError("the requested CPU quota must be lower than the original quota")
        return (f"{quota} {period}" if version == 2 else str(quota)), period

    if profile.startswith("memory-"):
        page_size = os.sysconf("SC_PAGE_SIZE")
        if value is None:
            if version == 1 and profile == "memory-reclaim":
                raise ValueError("v1 reclaim requires an explicit memory limit in bytes")
            usage = int(_read(directory, _memory_usage_file(version)))
            value = usage * 3 // 4 if profile == "memory-reclaim" else usage // 2
            value = max(page_size, value)
            if before != "max":
                value = min(value, int(before) - 1)
            if profile == "memory-reclaim":
                maximum = _read(directory, "memory.max")
                if maximum != "max":
                    value = min(value, int(maximum) - 1)
        # The kernel accounts limits in whole pages. Record the exact value it will expose.
        limit = _positive_int(value, "memory limit in bytes", page_size) // page_size * page_size
    else:
        if value is None:
            value = int(_read(directory, "pids.current"))
            if before != "max":
                value = min(value, int(before) - 1)
        limit = _positive_int(value, "task limit", 0)
    if before != "max" and limit >= int(before):
        raise ValueError("the requested limit must be lower than the original limit")
    return str(limit), None


def _memory_usage_file(version):
    return "memory.current" if version == 2 else "memory.usage_in_bytes"


def _guard_reclaim(directory, version, value):
    if version == 1:
        if int(value) < int(_read(directory, "memory.usage_in_bytes")):
            raise ValueError("v1 reclaim limit must cover the target's current memory usage")
    else:
        maximum = _read(directory, "memory.max")
        if maximum != "max" and int(value) >= int(maximum):
            raise ValueError("memory.high must be below the target's hard memory limit")


def prepare(profile, target):
    """Resolve the controller and capture the original limit before any mutation."""
    controller = CONTROLLERS[profile]
    pid = _positive_int(target.get("pid"), "target PID", 2)
    if pid == os.getpid():
        raise ValueError("target PID must not be the injector")
    if Path("/proc/self").resolve().name != str(os.getpid()):
        raise ValueError("the injector must use procfs for its current PID namespace")
    started, owner = _pid_identity(pid)
    if os.geteuid() not in (0, owner):
        raise ValueError(f"target PID {pid} is not owned by the caller")
    version, member = _membership(pid, controller)
    path, mount, root = _resolve(pid, controller, (version, member))
    if member == PurePosixPath("/") or path == mount:
        raise ValueError("a cgroup namespace or mount root cannot be a fault target")
    _guard_injector(path, mount, root, version, controller)

    if profile == "cpu-low":
        name = "cpu.max" if version == 2 else "cpu.cfs_quota_us"
    elif profile == "pids-low":
        name = "pids.max"
    elif version == 2:
        name = "memory.high" if profile == "memory-reclaim" else "memory.max"
    else:
        name = "memory.limit_in_bytes"
        if profile == "memory-reclaim" and target.get("v1_reclaim_via_limit") is not True:
            raise ValueError("v1 reclaim requires an explicit v1_reclaim_via_limit opt-in for its hard limit")

    with _directory(path) as directory:
        identity = os.fstat(directory)
        members = _guard_group(directory, pid, owner)
        if _pid_identity(pid) != (started, owner) or _membership(pid, controller) != (version, member):
            raise ValueError(f"target PID {pid} changed during cgroup selection")
        before = _read(directory, name)
        value, period = _requested_setting(profile, target.get("value"), directory, version, before)
        if profile == "memory-reclaim":
            _guard_reclaim(directory, version, value)

    step = {
        "backend": "cgroup",
        "profile": profile,
        "resource": f"cgroup:{identity.st_dev}:{identity.st_ino}:{controller}",
        "cgroup": str(path),
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "version": version,
        "file": name,
        "before": before,
        "value": value,
        "pid": pid,
        "started": started,
        "owner": owner,
        "member": str(member),
        "selected_pids": members,
    }
    if period is not None:
        step["period"] = period
    return step


@contextmanager
def _open_step(step):
    with _directory(step["cgroup"]) as directory:
        actual = os.fstat(directory)
        if (actual.st_dev, actual.st_ino) != (step["device"], step["inode"]):
            raise ValueError(f"cgroup identity changed for {step['cgroup']}")
        if step["file"] == "cpu.cfs_quota_us" and int(_read(directory, "cpu.cfs_period_us")) != step["period"]:
            raise ValueError(f"CPU quota period changed for {step['cgroup']}")
        yield directory


def _observation(directory, step, setting):
    controller = CONTROLLERS[step["profile"]]
    if controller == "cpu":
        names = ("cpu.stat", "cpuacct.usage")
    elif controller == "memory":
        names = (_memory_usage_file(step["version"]), "memory.stat", "memory.events", "memory.oom_control")
    else:
        names = ("pids.current", "pids.events")
    metrics = {}
    for name in names:
        try:
            metrics[name] = _read(directory, name)
        except (FileNotFoundError, PermissionError):
            continue  # Some kernels/controllers omit these supplementary counters.
    result = {"cgroup": step["cgroup"], "version": step["version"], "file": step["file"],
              "setting": setting, "metrics": metrics}
    if "pid" in step:
        result.update(target_pid=step["pid"], affected_pids=step["selected_pids"])
    return result


def observe(step):
    with _open_step(step) as directory:
        return _observation(directory, step, _read(directory, step["file"]))


def apply(step):
    with _open_step(step) as directory:
        controller = CONTROLLERS[step["profile"]]
        path = Path(step["cgroup"])
        mount, root, version, _ = _mount_for(path, controller, _mounts())
        if version != step["version"]:
            raise ValueError(f"cgroup hierarchy changed for {path}")
        if path == mount:
            raise ValueError(f"the cgroup mount root {path} cannot be a fault target")
        _guard_injector(path, mount, root, version, controller)
        name = step["file"]
        if _read(directory, name) != step["before"]:
            raise ValueError(f"{name} changed before injection on {step['cgroup']}")
        if step["profile"] == "memory-reclaim":
            _guard_reclaim(directory, version, step["value"])
        if "pid" in step:
            pid = step["pid"]
            if (_pid_identity(pid) != (step["started"], step["owner"])
                    or _membership(pid, controller) != (version, PurePosixPath(step["member"]))):
                raise ValueError(f"target PID {pid} or its cgroup membership changed before injection")
            step["selected_pids"] = _guard_group(directory, pid, step["owner"])
        _write(directory, name, step["value"])
        current = _read(directory, name)
        if current != step["value"]:
            raise ValueError(f"{name} readback differs from the requested limit: {current}")
        return _observation(directory, step, current)


def restore(step):
    with _open_step(step) as directory:
        name = step["file"]
        current = _read(directory, name)
        if current == step["value"]:
            _write(directory, name, step["before"])
            current = _read(directory, name)
        if current != step["before"]:
            raise ValueError(f"{name} does not match the injected or original value on {step['cgroup']}: {current}")
        return _observation(directory, step, current)
