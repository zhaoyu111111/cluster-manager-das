# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Arm a timed, per-device HIP fault in explicitly wrapped workloads."""

import hashlib
import os
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


DELAY_MS = 350
SOURCE = Path(__file__).with_name("hcu_operation.c")


def _directory(state_dir):
    directory = Path(state_dir) / "hip-hook"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("HIP hook directory must be owned by the caller with mode 0700")
    return directory


def execute(state_dir, command):
    directory = _directory(state_dir)
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
    library = directory / f"hip-{digest}.so"
    if not library.is_file():
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".so") as output:
            try:
                result = subprocess.run(
                    ["cc", "-std=c11", "-O2", "-shared", "-fPIC", "-Wall", "-Wextra",
                     str(SOURCE), "-o", output.name, "-ldl", "-pthread"],
                    check=False, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("compiling the HIP hook timed out") from error
            except FileNotFoundError as error:
                raise RuntimeError("wrap requires a C compiler named cc") from error
            if result.returncode:
                raise RuntimeError(f"compiling the HIP hook failed: {result.stderr.strip()}")
            # Publish the complete library without removing a concurrently published copy.
            try:
                os.link(output.name, library)
            except FileExistsError:
                pass
    if any(character in str(library) for character in ": \t\n"):
        raise ValueError("wrap requires a state directory without spaces or colons")
    environment = os.environ.copy()
    environment["HCU_FAULT_HOOK_DIR"] = str(directory)
    existing = environment.get("LD_PRELOAD")
    environment["LD_PRELOAD"] = f"{library}:{existing}" if existing else str(library)
    os.execvpe(command[0], command, environment)


def prepare(step, target):
    directory = _directory(target["state_dir"])
    if not any(directory.glob("hip-*.so")):
        raise ValueError("start the workload with wrap -- COMMAND using the same account and state directory")
    token = uuid.uuid4().hex
    step.update(
        rule=str(directory / f"{step['bdf']}.rule"),
        event=str(directory / f"{token}.hit"),
        token=token,
        operation="delay" if step["profile"] == "hcu-op-delay" else "error",
    )


def _owner(rule):
    try:
        return rule.read_text(encoding="ascii").split(maxsplit=1)[0]
    except FileNotFoundError:
        return None
    except IndexError:
        raise ValueError(f"empty HIP fault rule at {rule}") from None


def apply(step):
    rule = Path(step["rule"])
    event = Path(step["event"])
    event.touch(mode=0o600, exist_ok=False)
    expires = time.monotonic_ns() + step["seconds"] * 1_000_000_000
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=rule.parent) as file:
        file.write(f"{step['token']} {expires} {step['operation']} {DELAY_MS}\n")
        file.flush()
        try:
            os.link(file.name, rule)
        except FileExistsError as error:
            raise ValueError(f"HIP fault rule already exists for HCU {step['hcu']}") from error
    return observe(step)


def observe(step):
    rule = Path(step["rule"])
    event = Path(step["event"])
    try:
        hit = event.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        hit = ""
    return {
        "operation": step["operation"],
        "armed": _owner(rule) == step["token"],
        "first_hit": hit or None,
    }


def restore(step):
    rule = Path(step["rule"])
    result = observe(step)
    if _owner(rule) == step["token"]:
        rule.unlink()
        result["armed"] = False
    Path(step["event"]).unlink(missing_ok=True)
    return result
