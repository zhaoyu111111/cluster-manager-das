# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Discover and control caller-owned RDMA test processes and QP counters."""

import json
import os
import re
import secrets
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


_HCA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
_PROC = Path("/proc")
_IB = Path("/sys/class/infiniband")
_PORT_COUNTERS = ("port_xmit_data", "port_rcv_data", "port_xmit_packets", "port_rcv_packets")
_EXIT_STATES = {"X", "x", "Z"}
_STOP_STATES = {"T", "t"}


def _rdma(*args, read=True):
    command = ["rdma", *(("-j",) if read else ()), *map(str, args)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=4)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"rdma {' '.join(map(str, args))} timed out") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"rdma {' '.join(map(str, args))}: {detail[-480:]}")
    if not read:
        return None
    rows = json.loads(result.stdout) if result.stdout.strip() else []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("unexpected rdma JSON readback")
    return rows


def _proc_stat(pid):
    fields = (_PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[1]), int(fields[19])


def _namespace(pid, name):
    info = (_PROC / str(pid) / "ns" / name).stat()
    return [info.st_dev, info.st_ino]


def _process(pid):
    proc = _PROC / str(pid)
    state, _, start = _proc_stat(pid)
    if state in _EXIT_STATES:
        raise ProcessLookupError(f"RDMA test PID {pid} exited")
    identity = {"uid": proc.stat().st_uid, "start_time": start,
                "pid_ns": _namespace(pid, "pid"), "net_ns": _namespace(pid, "net")}
    current, _, after = _proc_stat(pid)
    if start != after or current in _EXIT_STATES:
        raise ProcessLookupError(f"RDMA test PID {pid} changed during inspection")
    return current, identity


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        raise ValueError("use an RDMA test PID greater than 1")
    try:
        _, identity = _process(pid)
    except FileNotFoundError as error:
        raise ProcessLookupError(f"RDMA test PID {pid} is absent") from error
    if (identity["uid"] != os.geteuid() or identity["pid_ns"] != _namespace("self", "pid")
            or identity["net_ns"] != _namespace("self", "net")):
        raise ValueError("use a caller-owned RDMA test process in the same PID and network namespaces")
    return identity


def _live_state(step):
    identity = step["process_identity"]
    if identity["pid_ns"] != _namespace("self", "pid") or identity["net_ns"] != _namespace("self", "net"):
        raise ValueError("run restore in the original RDMA test namespaces")
    try:
        state, current = _process(step["pid"])
    except (FileNotFoundError, ProcessLookupError):
        return None
    if current["start_time"] != identity["start_time"]:
        return None
    if current != identity or current["uid"] != os.geteuid():
        raise ValueError("RDMA test process ownership or namespace changed")
    return state


def _qps(pid, link=None, qpn=None):
    selector = ("link", link) if link is not None else ()
    args = ("resource", "show", "qp", *selector, "pid", pid)
    if link is not None and qpn is not None:
        try:
            rows = _rdma(*args, "lqpn", qpn)
        except RuntimeError:
            # The direct kernel lookup can fail when the QP has already gone.
            rows = _rdma(*args)
    else:
        rows = _rdma(*args)
    qps = []
    for row in rows:
        device = row.get("ifname")
        port = row.get("port")
        number = row.get("lqpn")
        if (row.get("pid") != pid or not isinstance(device, str) or not _HCA.fullmatch(device)
                or type(port) is not int or not 1 <= port <= 255
                or type(number) is not int or not 0 <= number < 1 << 24):
            continue
        qps.append({"link": f"{device}/{port}", "device": device, "port": port, "qpn": number,
                    "device_index": row.get("ifindex"), "remote_qpn": row.get("rqpn"),
                    "pdn": row.get("pdn"), "state": row.get("state"), "type": row.get("type")})
    return sorted(qps, key=lambda item: (item["link"], item["qpn"]))


def _port_snapshot(item):
    port = _IB / item["device"] / "ports" / str(item["port"])
    result = {"link": item["link"], "counter_scope": "port_total"}
    for field in ("state", *_PORT_COUNTERS):
        path = port / field if field == "state" else port / "counters" / field
        try:
            value = path.read_text(encoding="ascii").strip()
            result[field] = value if field == "state" else int(value)
        except (OSError, ValueError):
            result[field] = None
    return result


def paths(pid, link=None, qpn=None):
    identity = process_identity(pid)
    qps = _qps(pid, link, qpn)
    links = {item["link"]: item for item in qps}
    ports = [_port_snapshot(item) for item in links.values()]
    if process_identity(pid) != identity:
        raise ProcessLookupError(f"RDMA test PID {pid} changed during discovery")
    return {"pid": pid, "qps": qps, "ports": ports}


def qp(pid, qpn, link=None):
    matches = [item for item in paths(pid, link, qpn)["qps"]
               if item["qpn"] == qpn and (link is None or item["link"] == link)]
    if len(matches) != 1:
        raise ValueError(f"expected one local QP {qpn} for PID {pid}, found {len(matches)}; use PID:QPN@HCA/PORT if needed")
    return matches[0]


def _qp_identity(item):
    return {name: item[name] for name in ("link", "device_index", "qpn", "remote_qpn", "pdn", "type")}


def _current_qp(step):
    if _live_state(step) is None:
        return None
    link = step["qp_identity"]["link"]
    matches = [item for item in _qps(step["pid"], link, step["qpn"])
               if item["link"] == link and item["qpn"] == step["qpn"]]
    if _live_state(step) is None:
        return None
    if len(matches) > 1 or any(_qp_identity(item) != step["qp_identity"] for item in matches):
        raise ValueError("RDMA QP identity changed")
    return matches[0] if matches else None


def _binding(step):
    link = step["qp_identity"]["link"]
    matches = []
    for row in _rdma("statistic", "qp", "show", "link", link):
        numbers = row.get("lqpn")
        if type(numbers) is int:
            numbers = [numbers]
        if not isinstance(numbers, list) or type(row.get("cntn")) is not int:
            raise ValueError("cannot verify the current RDMA QP counter bindings")
        if step["qpn"] not in numbers:
            continue
        # Linux emits the counter PID only for automatic PID-based counters.
        # A manual counter is scoped by the independently verified live QP.
        if numbers != [step["qpn"]] or row.get("pid") not in (None, step["pid"]):
            raise ValueError("selected RDMA QP belongs to a shared or foreign counter")
        matches.append(row)
    if len(matches) > 1:
        raise ValueError("selected RDMA QP has ambiguous counter bindings")
    return matches[0] if matches else None


def _counter_mode(link):
    rows = _rdma("statistic", "qp", "mode", "link", link)
    if len(rows) != 1 or rows[0].get("mode") != "qp auto off":
        raise ValueError(f"{link} must have automatic QP counter binding disabled")


def _check_not_ancestor(pid):
    current = os.getpid()
    while current > 1:
        if current == pid:
            raise ValueError("the target must be an independent RDMA test process")
        _, current, _ = _proc_stat(current)


def _require_pidfd():
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ValueError("the process-pause profile requires Linux pidfd support")
    os.close(os.pidfd_open(os.getpid()))


def _arm(step):
    if "intent_file" in step:
        os.close(os.open(step["intent_file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    step["armed"] = True


def _armed(step):
    return step.get("armed", False) or (
        "intent_file" in step and Path(step["intent_file"]).is_file()
    )


def _disarm(step):
    if "intent_file" in step:
        Path(step["intent_file"]).unlink(missing_ok=True)
    step["armed"] = False


def prepare(step):
    pid = step["pid"]
    identity = process_identity(pid)
    step["process_identity"] = identity
    step["armed"] = False
    if step.get("state_dir"):
        step["intent_file"] = str(Path(step["state_dir"]) / f"rdma-{secrets.token_hex(16)}.intent")
    if step["profile"] == "rdma-send-stop":
        _check_not_ancestor(pid)
        _require_pidfd()
        if _live_state(step) in _STOP_STATES:
            raise ValueError("RDMA test process is already stopped")
        if not any(item["state"] == "RTS" for item in _qps(pid)):
            raise ValueError("RDMA test process has no ready-to-send QP on a local port")
        step["resource"] = f"rdma-process:{identity['pid_ns'][1]}:{pid}:{identity['start_time']}"
    else:
        item = qp(pid, step["qpn"], step.get("link"))
        step["qp_identity"] = _qp_identity(item)
        _counter_mode(item["link"])
        if _binding(step) is not None:
            raise ValueError("selected RDMA QP already has a counter binding")
        step["resource"] = f"rdma-qp:{identity['net_ns'][1]}:{item['link']}:{item['qpn']}"
    if _live_state(step) is None:
        raise ProcessLookupError(f"RDMA test PID {pid} changed during preparation")
    return step


@contextmanager
def _pidfd(step):
    _require_pidfd()
    try:
        fd = os.pidfd_open(step["pid"])
    except ProcessLookupError:
        yield None
        return
    try:
        yield fd if _live_state(step) is not None else None
    finally:
        os.close(fd)


def _wait_state(step, stopped):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = _live_state(step)
        if state is None or (state == "T" if stopped else state not in _STOP_STATES):
            return state
        time.sleep(0.02)
    raise RuntimeError("RDMA test process did not reach the requested stop state")


def observe(step):
    state = _live_state(step)
    result = {"pid": step["pid"], "process_state": state, "process_alive": state is not None}
    if step["profile"] == "rdma-send-stop":
        return {**result, "paused": state in _STOP_STATES,
                "qps": _qps(step["pid"]) if state is not None else []}
    item = _current_qp(step)
    return {**result, "qp": item, "counter": _binding(step) if item is not None else None}


def apply(step):
    if step["profile"] == "rdma-send-stop":
        with _pidfd(step) as fd:
            state = _live_state(step)
            if fd is None or state is None or state in _STOP_STATES:
                raise ValueError("RDMA test process exited or was stopped before injection")
            if not any(item["state"] == "RTS" for item in _qps(step["pid"])):
                raise ValueError("RDMA test process no longer has a ready-to-send QP")
            _arm(step)
            try:
                signal.pidfd_send_signal(fd, signal.SIGSTOP)
            except OSError:
                _disarm(step)
                raise
            if _wait_state(step, stopped=True) is None:
                raise ProcessLookupError("RDMA test process exited during pause")
    else:
        item = _current_qp(step)
        if item is None or _binding(step) is not None:
            raise ValueError("selected QP or its counter binding changed before injection")
        _counter_mode(item["link"])
        if _current_qp(step) is None:
            raise ValueError("selected QP disappeared before counter binding")
        _arm(step)
        _rdma("statistic", "qp", "bind", "link", item["link"], "lqpn", item["qpn"], read=False)
        bound = _binding(step)
        if bound is None or _current_qp(step) is None:
            raise RuntimeError("kernel did not report the requested RDMA QP counter binding")
        step["counter_id"] = bound["cntn"]
    return observe(step)


def restore(step):
    if not _armed(step):
        return observe(step)
    if step["profile"] == "rdma-send-stop":
        with _pidfd(step) as fd:
            state = _live_state(step)
            if state == "t":
                raise ValueError("RDMA test process is stopped by a tracer")
            if fd is not None and state is not None:
                try:
                    # Also cancel a pending SIGSTOP that has not reached a task in D state.
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                _wait_state(step, stopped=False)
    else:
        item = _current_qp(step)
        bound = _binding(step)
        if bound is not None:
            if (item is None or step.get("counter_id", bound["cntn"]) != bound["cntn"]
                    or _current_qp(step) is None):
                raise ValueError("refusing to unbind a changed RDMA QP or counter")
            _rdma("statistic", "qp", "unbind", "link", item["link"], "cntn", bound["cntn"],
                  "lqpn", item["qpn"], read=False)
            if _binding(step) is not None:
                raise RuntimeError("kernel still reports a counter on the selected RDMA QP")
    result = observe(step)
    _disarm(step)
    return result
