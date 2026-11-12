# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from backends import hcu, rdma
from fault_profiles import PROFILES, target_options, validate_target
from fault_sessions import SessionStore, launch, supervise


def duration(value):
    value = value.removesuffix("s")
    if not value.isascii() or not value.isdecimal() or len(value) > 3 or not 1 <= int(value) <= 600:
        raise argparse.ArgumentTypeError("use a duration of 1 to 600 seconds, such as 30s")
    return int(value)


def cpu_list(value):
    try:
        return [int(cpu) for cpu in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("use comma-separated CPU numbers, such as 0,1") from None


def default_state():
    if os.geteuid() == 0:
        return Path("/var/lib/hcu-fault-inject")
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "hcu-fault-inject"


def options():
    parser = argparse.ArgumentParser(description="Start and stop scoped resource faults")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list fault profiles and their target scope")
    devices = commands.add_parser("devices", help="list local HCU numbers and PCI addresses")
    start = commands.add_parser("start", aliases=["apply"], help="start a fault on a PID, HCU or test path")
    start.set_defaults(command="start")
    stop = commands.add_parser("stop", help="restore a fault by its profile and original target")
    status = commands.add_parser("status", help="show a session and its readback")
    restore = commands.add_parser("restore", help="restore a fault session")
    wrap = commands.add_parser("wrap", help="start a workload with opt-in HIP operation injection")
    rdma_wrap = commands.add_parser("rdma-wrap", help="start an opt-in verbs workload")
    paths = commands.add_parser("rdma-path", help="read a workload's RDMA queue pairs and port counters")
    rccl = commands.add_parser("rccl-run", help="run a command with exact RCCL HCA port selection")
    listener = commands.add_parser("rdma-listen", help="authorize an isolated RDMA contention peer")
    supervisor = commands.add_parser("_supervise", help=argparse.SUPPRESS)
    for command in (start, stop, status, restore, wrap, rdma_wrap, supervisor):
        command.add_argument("--state-dir", type=Path)
    for command in (listing, devices, start, stop, status, restore):
        command.add_argument("--json", action="store_true")
    for command in (start, stop):
        command.add_argument("profile", choices=PROFILES)
        command.add_argument("target", metavar="TARGET")
    start.add_argument("--for", dest="duration", type=duration, default=30, help="duration, default 30s")
    start.add_argument("--value", help="override a cgroup limit, power cap, TCP delay or transmit rate")
    start.add_argument("--cpus", type=cpu_list, help="override affinity or NUMA helper CPUs")
    start.add_argument("--cpu", type=int, help="override the cpu-contend CPU")
    start.add_argument("--mib", type=int, help="override MiB per numa-bandwidth helper")
    start.add_argument("--v1-reclaim-via-limit", action="store_true",
                       help="opt in to a hard-limit reclaim method on cgroup v1")
    status.add_argument("session_id", nargs="?")
    restore.add_argument("session_id")
    for command in (wrap, rdma_wrap, rccl):
        if command is rccl:
            command.add_argument("links", metavar="HCA/PORT[,HCA/PORT]")
        command.add_argument("workload", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARGS ...]")
    paths.add_argument("pid", metavar="PID")
    listener.add_argument("link", metavar="HCA/PORT")
    listener.add_argument("--bind", required=True, help="local private test IPv4")
    listener.add_argument("--peer", required=True, help="authorized private sender IPv4")
    supervisor.add_argument("session_id")
    return parser


def workload_command(args):
    command = args.workload
    if command[:1] == ["--"]:
        command = command[1:]
    elif args.command != "rccl-run":
        command = []
    if not command:
        raise ValueError(f"{args.command} requires -- COMMAND [ARGS ...]")
    return command


def display(data, as_json):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, list):
        for row in data:
            if "session_id" in row:
                print(row["session_id"], row["profile"], row["target"], row["status"])
            elif "hcu" in row:
                print(row["hcu"], row["bdf"])
            else:
                print(row["profile"], row["scope"])
    else:
        print(*(data[key] for key in ("session_id", "profile", "target", "status") if key in data))
        for key in ("error", "restore_error", "observation_error", "observed", "current", "restored"):
            if key in data:
                print(key, json.dumps(data[key], ensure_ascii=False))


def main(argv=None):
    if sys.platform != "linux":
        print("hcu-fault-inject requires Linux", file=sys.stderr)
        return 1
    args = options().parse_args(argv)
    try:
        if args.command == "list":
            scope = {"cgroup": "cgroup", "resource": "process", "hcu": "hcu", "ip-network": "network",
                     "rdma": "rdma"}
            display([{"profile": name, "scope": scope[kind]}
                     for name, kind in PROFILES.items()], args.json)
            return 0
        if args.command == "devices":
            display(hcu.devices(), args.json)
            return 0
        if args.command == "rdma-path":
            display(rdma.read_paths(args.pid), True)
            return 0
        if args.command == "rccl-run":
            rdma.rccl_run(args.links, workload_command(args))
            return 0
        if args.command == "rdma-listen":
            from backends import rdma_congestion

            rdma_congestion.listen(args.link, args.bind, args.peer)
            return 0
        if args.command in {"start", "stop"}:
            input_target = args.target
            args.target = validate_target(args.profile, input_target, stopping=args.command == "stop")
        store = SessionStore(args.state_dir or default_state())
        if args.command == "wrap":
            from backends import hcu_operation

            hcu_operation.execute(store.directory, workload_command(args))
            return 0
        if args.command == "rdma-wrap":
            from backends import rdma_hook

            rdma_hook.execute(store.directory, workload_command(args))
            return 0
        if args.command == "_supervise":
            supervise(store, args.session_id)
            return 0
        if args.command == "start":
            target = target_options(args, input_target)
            if PROFILES[args.profile] in {"hcu", "rdma"}:
                target["state_dir"] = str(store.directory)
            result = launch(store, args.profile, args.target, target, args.duration)
        elif args.command == "stop":
            result = store.stop(args.profile, args.target)
        elif args.command == "restore":
            result = store.restore(args.session_id)
        else:
            result = store.status(args.session_id)
        display(result, args.json)
        if isinstance(result, dict) and result["status"] in {"apply_failed", "restore_failed"}:
            return 1
        return 0
    except KeyboardInterrupt:
        if args.command in {"start", "stop"}:
            if args.profile in rdma.CONGESTION_PROFILES and not rdma.SAVED_PEER.fullmatch(str(args.target)):
                print(f"Interrupted. Use stop {args.profile} with the original test peer selector.", file=sys.stderr)
                return 130
            stop_args = ["stop", args.profile, str(args.target)]
            if args.state_dir is not None:
                stop_args.extend(("--state-dir", str(args.state_dir.absolute())))
            print(f"Interrupted. Use {shlex.join(stop_args)} to restore any active session.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
        else:
            print(str(error), file=sys.stderr)
        return 1
