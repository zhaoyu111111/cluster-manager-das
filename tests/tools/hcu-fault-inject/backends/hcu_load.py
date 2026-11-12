# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Timed HIP load processes for single-device compute and memory contention."""

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .resource import _CHILDREN, _matches, _require_helper_control, _stat, _stop_helper


MODES = {"hcu-compute-contend": "compute", "hcu-memory-contend": "memory"}
SOURCE = Path(__file__).with_suffix(".hip")
START_TIMEOUT = 12
COMPILE_TIMEOUT = 45


def prepare(step):
    _require_helper_control()
    compiler = shutil.which("hipcc") or shutil.which("hipcc", path="/opt/dtk/bin:/opt/dtk/hip/bin:/opt/rocm/bin")
    if compiler is None:
        raise ValueError("HIP contention requires hipcc from the device's HIP toolchain")
    step.update(helper=None, compiler=str(Path(compiler).absolute()), mode=MODES[step["profile"]])


def _compile(compiler, executable):
    try:
        result = subprocess.run(
            [compiler, "-O3", "-std=c++17", "-x", "hip", str(SOURCE), "-o", str(executable)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            timeout=COMPILE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("HIP load helper compilation timed out") from error
    if result.returncode:
        raise RuntimeError(f"HIP load helper compilation failed: {result.stdout.strip()[-2000:]}")


def _read_ready(pipe):
    deadline = time.monotonic() + START_TIMEOUT
    payload = b""
    while len(payload) < 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([pipe], [], [], remaining)[0]:
            break
        chunk = os.read(pipe.fileno(), 4096)
        if not chunk:
            break
        payload += chunk
        if b"\n" in payload:
            return json.loads(payload.split(b"\n", 1)[0])
    return {}


def apply(step):
    with tempfile.TemporaryDirectory(prefix="hcu-fault-load-") as directory:
        executable = Path(directory) / "hcu-load"
        _compile(step["compiler"], executable)
        with tempfile.TemporaryFile() as errors:
            # Leave the supervisor enough time to finish the readiness handshake.
            lifetime = step["seconds"] + 2
            process = subprocess.Popen(
                [str(executable), step["mode"], step["bdf"], str(lifetime), str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
            )
            try:
                _, start, _ = _stat(process.pid)
                step["helper"] = {"pid": process.pid, "start": start}
                _CHILDREN[process.pid] = process
                with process.stdout:
                    sample = _read_ready(process.stdout)
                if (sample.get("ready") is not True or sample.get("mode") != step["mode"]
                        or sample.get("bdf", "").lower() != step["bdf"].lower()
                        or sample.get("throughput", 0) <= 0):
                    errors.seek(0)
                    detail = errors.read(2000).decode(errors="replace").strip()
                    raise RuntimeError(f"HIP load helper failed readiness check: {detail or 'no valid sample'}")
                step["helper"]["sample"] = sample
                evidence = observe(step)
                if not evidence["load_active"]:
                    raise RuntimeError("HIP load helper exited during startup")
            except Exception:
                if step["helper"] is None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                if step["helper"] is not None:
                    _stop_helper(step["helper"])
                raise
    return evidence


def observe(step):
    helper = step["helper"]
    result = {"hcu": step["hcu"], "bdf": step["bdf"], "load_active": False}
    if helper is not None:
        result.update(helper_pid=helper["pid"], load_active=_matches(helper["pid"], helper["start"]))
        if "sample" in helper:
            result["load_sample"] = helper["sample"]
    return result


def restore(step):
    if step["helper"] is not None:
        _stop_helper(step["helper"])
    result = observe(step)
    if result["load_active"]:
        raise RuntimeError(f"HIP load helper PID {result['helper_pid']} is still active")
    return result
