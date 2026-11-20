# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

import re

from backends import cgroup, hcu, ip_network, rdma, resource

BACKENDS = {"cgroup": cgroup, "resource": resource, "hcu": hcu, "ip-network": ip_network, "rdma": rdma}
PROFILES = {
    "cpu-low": "cgroup",
    "cpu-cores": "resource",
    "cpu-contend": "resource",
    "numa-remote": "resource",
    "numa-bandwidth": "resource",
    "memory-reclaim": "cgroup",
    "memory-oom": "cgroup",
    "pids-low": "cgroup",
    "clock-low": "hcu",
    "clock-high": "hcu",
    "clock-pulse": "hcu",
    "power-low": "hcu",
    "power-pulse": "hcu",
    "hcu-compute-contend": "hcu",
    "hcu-memory-contend": "hcu",
    "hcu-op-delay": "hcu",
    "hcu-op-error": "hcu",
    "rail-down": "ip-network",
    "rail-flap": "ip-network",
    "tcp-delay": "ip-network",
    "tcp-loss": "ip-network",
    "tcp-rate": "ip-network",
    "tcp-jitter": "ip-network",
    "tcp-reorder": "ip-network",
    "tcp-random-loss": "ip-network",
    "tcp-burst-loss": "ip-network",
    "tcp-drop": "ip-network",
    "tcp-reset": "ip-network",
    **dict.fromkeys(rdma.PROFILES, "rdma"),
}
MEMORY_PROFILES = {"memory-reclaim", "memory-oom"}
BYTE_UNITS = {"": 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def parse_value(profile, value):
    pattern = r"([0-9]{1,19})(B|KiB|MiB|GiB)?" if profile in MEMORY_PROFILES else r"([0-9]{1,19})"
    match = re.fullmatch(pattern, value)
    if match is None:
        raise ValueError("use bytes or a KiB/MiB/GiB suffix for memory" if profile in MEMORY_PROFILES
                         else "--value requires an integer")
    multiplier = BYTE_UNITS[match.group(2) or ""] if profile in MEMORY_PROFILES else 1
    return int(match.group(1)) * multiplier


def validate_target(profile, target, *, stopping=False):
    if PROFILES[profile] == "rdma":
        canonical, _ = rdma.parse_target(profile, target, stopping=stopping)
        return canonical
    if PROFILES[profile] == "ip-network":
        canonical, _ = ip_network.parse_target(profile, target)
        return canonical
    try:
        number = int(target)
    except ValueError:
        raise ValueError("target must be a numeric PID or HCU number") from None
    if PROFILES[profile] == "hcu":
        if number < 0:
            raise ValueError("HCU number must be nonnegative; use devices to list numbers")
    elif number <= 1:
        raise ValueError("PID must identify a workload process greater than 1")
    return number


def target_options(args, input_target=None):
    profile = args.profile
    backend = PROFILES[profile]
    if backend in {"hcu", "ip-network", "rdma"}:
        if any(option is not None for option in (args.cpus, args.cpu, args.mib)) or args.v1_reclaim_via_limit:
            raise ValueError("CPU, NUMA and cgroup options require a workload PID profile")
    if backend == "rdma":
        _, target = rdma.parse_target(profile, input_target if input_target is not None else args.target)
        if args.value is not None:
            if profile not in rdma.CONGESTION_PROFILES:
                raise ValueError("RDMA --value sets the test congestion rate in Gbit/s")
            target["value"] = parse_value(profile, args.value)
        return target
    if backend == "ip-network":
        _, target = ip_network.parse_target(profile, args.target)
        if args.value is not None:
            if profile not in ip_network.VALUE_PROFILES:
                raise ValueError("network --value is used by tcp-delay and tcp-rate")
            target["value"] = parse_value(profile, args.value)
        return target
    if backend == "hcu":
        target = {"hcu": args.target, "seconds": args.duration}
        if args.value is not None:
            if profile not in {"power-low", "power-pulse"}:
                raise ValueError("--value is only used by power and cgroup profiles")
            target["value"] = parse_value(profile, args.value)
        return target
    target = {"pid": args.target}
    if args.value is not None:
        if backend != "cgroup":
            raise ValueError("--value is only used by cgroup profiles")
        target["value"] = parse_value(profile, args.value)
    if args.cpus is not None:
        if profile not in {"cpu-cores", "numa-remote", "numa-bandwidth"}:
            raise ValueError("--cpus is only used by cpu-cores and NUMA profiles")
        target["cpus"] = args.cpus
    if args.cpu is not None:
        if profile != "cpu-contend":
            raise ValueError("--cpu is only used by cpu-contend")
        target["cpu"] = args.cpu
    if args.mib is not None:
        if profile != "numa-bandwidth":
            raise ValueError("--mib is only used by numa-bandwidth")
        target["mib"] = args.mib
    if args.v1_reclaim_via_limit:
        if profile != "memory-reclaim":
            raise ValueError("--v1-reclaim-via-limit is only used by memory-reclaim")
        target["v1_reclaim_via_limit"] = True
    return target
