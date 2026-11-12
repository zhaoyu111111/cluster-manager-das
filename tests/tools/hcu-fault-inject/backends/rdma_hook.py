# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Arm one fault on a QP owned by an explicitly wrapped verbs workload."""

import fcntl
import hashlib
import os
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import rdma_control


SOURCE = Path(__file__).with_name("rdma_hook.c")
OPERATIONS = {
    "rdma-qp-wait": ("wait", 200),
    "rdma-qp-error": ("error", 0),
    "rdma-cq-delay": ("cq", 550),
}


def _directory(state_dir):
    directory = Path(state_dir) / "rdma-hook"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("RDMA hook directory must be owned by the caller with mode 0700")
    return directory


def execute(state_dir, command):
    directory = _directory(state_dir)
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
    library = directory / f"verbs-{digest}.so"
    if any(character in str(library) for character in ": \t\n"):
        raise ValueError("rdma-wrap requires a state directory without spaces or colons")
    if not library.is_file():
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".so") as output:
            try:
                result = subprocess.run(
                    ["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-o", output.name,
                     str(SOURCE), "-ldl", "-pthread", "-libverbs"],
                    check=False, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("compiling the RDMA hook timed out") from error
            except FileNotFoundError as error:
                raise RuntimeError("rdma-wrap requires a C compiler named cc") from error
            if result.returncode:
                raise RuntimeError(f"compiling the RDMA hook failed: {result.stderr.strip()[-1600:]}")
            try:
                os.link(output.name, library)
            except FileExistsError:
                pass
    environment = os.environ.copy()
    environment["HCU_RDMA_HOOK_DIR"] = str(directory)
    existing = environment.get("LD_PRELOAD")
    environment["LD_PRELOAD"] = f"{library}:{existing}" if existing else str(library)
    os.execvpe(command[0], command, environment)


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"invalid RDMA hook file {path}")
        return stream.read(256).strip()


def _ready(pid, identity, directory):
    if identity["uid"] != os.geteuid():
        raise ValueError("RDMA hooks require a workload owned by the calling user")
    marker = _read(directory / f"{pid}.ready")
    environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    setting = os.fsencode(f"HCU_RDMA_HOOK_DIR={directory}")
    if marker != str(identity["start_time"]) or setting not in environment:
        raise ValueError("launch the verbs workload with rdma-wrap and the same state directory")


def prepare(step):
    pid, qpn = step["pid"], step["qpn"]
    identity = rdma_control.process_identity(pid)
    directory = _directory(step["state_dir"])
    _ready(pid, identity, directory)
    selected = rdma_control.qp(pid, qpn, step.get("link"))
    if selected.get("type") != "RC" or selected.get("state") != "RTS":
        raise ValueError("RDMA hook requires an owned RC queue pair in RTS")
    if identity != rdma_control.process_identity(pid):
        raise ValueError("workload PID changed while selecting the RDMA queue pair")
    token = uuid.uuid4().hex
    operation, value = OPERATIONS[step["profile"]]
    step.update(identity=identity, selected=selected, token=token,
                resource=f"rdma-hook:{identity['pid_ns']}:{pid}:{identity['start_time']}:{qpn}",
                rule=str(directory / f"{pid}-{qpn}.rule"), event=str(directory / f"{token}.hit"),
                operation=operation, value=value)


def _armed(step):
    return _read(step["rule"]).partition(" ")[0] == step["token"]


def _evidence(step, evidence, armed):
    status, _, detail = evidence.partition(" ")
    return {"pid": step["pid"], "qpn": step["qpn"], "link": step["selected"]["link"],
            "operation": step["operation"], "armed": armed, "hit_status": status or None,
            "hit_detail": detail or None}


def observe(step):
    return _evidence(step, _read(step["event"]), _armed(step))


def apply(step):
    pid, qpn = step["pid"], step["qpn"]
    rule = Path(step["rule"])
    if step["identity"] != rdma_control.process_identity(pid):
        raise ValueError("workload PID changed before RDMA fault")
    _ready(pid, step["identity"], rule.parent)
    if rdma_control.qp(pid, qpn, step["selected"]["link"]) != step["selected"]:
        raise ValueError("selected RDMA queue pair changed before injection")
    Path(step["event"]).touch(mode=0o600, exist_ok=False)
    expires = time.monotonic_ns() + step["seconds"] * 1_000_000_000
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=rule.parent) as file:
        file.write(f"{step['token']} {expires} {step['operation']} {step['value']} "
                   f"{step['identity']['start_time']} {step['selected']['device']}\n")
        file.flush()
        try:
            os.link(file.name, rule)
        except FileExistsError as error:
            raise ValueError(f"an RDMA rule already exists for PID {pid}, QPN {qpn}") from error
    return observe(step)


def restore(step):
    rule, event = Path(step["rule"]), Path(step["event"])
    if _armed(step):
        rule.unlink()
    evidence = ""
    try:
        fd = os.open(event, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        fd = None
    if fd is not None:
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("invalid RDMA hook evidence file")
            # Wait for any one-shot operation which already claimed this file.
            until = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise RuntimeError("RDMA hook is still completing the selected operation")
                    time.sleep(0.02)
            evidence = stream.read(256).strip()
            event.unlink(missing_ok=True)
    result = _evidence(step, evidence, False)
    if step["operation"] == "error" and (
        result["hit_status"] == "applied" or evidence.startswith("error qp_err_readback_failed")
    ):
        result["qp_recovery"] = "recreate the affected queue pair"
    return result
