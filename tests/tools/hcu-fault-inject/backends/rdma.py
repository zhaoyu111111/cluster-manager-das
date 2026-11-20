# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Scoped native RDMA controls and RCCL launch-time HCA selection."""

import json
import os
import re
import subprocess


CONTROL_PROFILES = ("rdma-send-stop", "rdma-counter")
HOOK_PROFILES = ("rdma-qp-wait", "rdma-qp-error", "rdma-cq-delay")
CONGESTION_PROFILES = ("rdma-congest", "rdma-burst")
PROFILES = (*CONTROL_PROFILES, *HOOK_PROFILES, *CONGESTION_PROFILES)
LINK = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_.-]{0,62})/([1-9][0-9]{0,2})")
QP_TARGET = re.compile(r"([0-9]{1,10}):([0-9]{1,8})")
SAVED_PEER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}/[1-9][0-9]{0,2}"
                        r"@[0-9.]{7,15}:[0-9]{1,5}#[0-9a-f]{16}")


def pid_number(target):
    if not re.fullmatch(r"[0-9]{1,10}", str(target)) or not 1 < int(target) <= 2**31 - 1:
        raise ValueError("target must be a workload PID greater than 1")
    return int(target)


def parse_target(profile, target, *, stopping=False):
    if profile == "rdma-send-stop":
        pid = pid_number(target)
        return pid, {"pid": pid}
    if profile in CONGESTION_PROFILES:
        if stopping and SAVED_PEER.fullmatch(target):
            return target, {}
        from . import rdma_congestion

        return rdma_congestion.parse_target(target)
    endpoint, separator, link = target.partition("@")
    match = QP_TARGET.fullmatch(endpoint)
    if not match:
        raise ValueError(f"{profile} takes PID:QPN or PID:QPN@HCA/PORT")
    pid, qpn = pid_number(match[1]), int(match[2])
    if not 0 < qpn < 2**24:
        raise ValueError("QPN must identify a non-management RDMA queue pair")
    if separator:
        port = LINK.fullmatch(link)
        if not port or int(port[2]) > 255:
            raise ValueError("RDMA link must be HCA/PORT, such as shca_0/1")
        return f"{pid}:{qpn}@{link}", {"pid": pid, "qpn": qpn, "link": link}
    return f"{pid}:{qpn}", {"pid": pid, "qpn": qpn}


def _backend(profile):
    if profile in CONTROL_PROFILES:
        from . import rdma_control

        return rdma_control
    if profile in CONGESTION_PROFILES:
        from . import rdma_congestion

        return rdma_congestion
    from . import rdma_hook

    return rdma_hook


def prepare(profile, target):
    step = {"profile": profile, **target}
    _backend(profile).prepare(step)
    return step


def observe(step):
    return _backend(step["profile"]).observe(step)


def apply(step):
    return _backend(step["profile"]).apply(step)


def restore(step):
    return _backend(step["profile"]).restore(step)


def tick(step, elapsed):
    if step["profile"] in CONGESTION_PROFILES:
        return _backend(step["profile"]).tick(step, elapsed)
    return False


def read_paths(pid):
    from . import rdma_control

    return rdma_control.paths(pid_number(pid))


def active_link(link):
    match = LINK.fullmatch(link)
    if not match or int(match[2]) > 255:
        raise ValueError("RDMA link must be HCA/PORT, such as shca_0/1")
    try:
        result = subprocess.run(["rdma", "-j", "link", "show", link], capture_output=True,
                                text=True, timeout=5, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"timed out reading RDMA link {link}") from error
    if result.returncode:
        raise ValueError(f"cannot read RDMA link {link}: {result.stderr.strip()}")
    rows = json.loads(result.stdout)
    if (len(rows) != 1 or rows[0].get("ifname") != match[1]
            or rows[0].get("port") != int(match[2]) or rows[0].get("state") != "ACTIVE"):
        raise ValueError(f"RDMA link {link} is not an active local port")
    return match[1], match[2]


def rccl_run(links, command):
    selected = links.split(",")
    if len(selected) != len(set(selected)) or len(selected) > 16:
        raise ValueError("select up to 16 distinct local RDMA ports")
    environment = os.environ.copy()
    environment["NCCL_IB_HCA"] = "=" + ",".join(":".join(active_link(link)) for link in selected)
    os.execvpe(command[0], command, environment)
