# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Reversible link and TCP faults on dedicated test interfaces."""

import json
import re
import subprocess
from pathlib import Path


LINK_PROFILES = {"rail-down", "rail-flap"}
FLOW_PROFILES = {
    "tcp-delay", "tcp-loss", "tcp-jitter", "tcp-reorder", "tcp-random-loss",
    "tcp-burst-loss", "tcp-drop", "tcp-reset",
}
PROFILES = LINK_PROFILES | FLOW_PROFILES | {"tcp-rate"}
VALUE_PROFILES = {"tcp-delay", "tcp-rate"}
NETNS = Path("/proc/self/ns/net")
INTERFACE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}")
PORT = re.compile(r"[0-9]{1,5}")
TEST_ALIAS = "hcu-fault-inject"
FLAP_SECONDS = 2


def parse_target(profile, target):
    parts = target.split(":")
    device = parts[0]
    if device == "lo" or not INTERFACE.fullmatch(device):
        raise ValueError("use a dedicated test interface name such as fi-veth0")
    if profile not in FLOW_PROFILES:
        if len(parts) != 1:
            raise ValueError(f"{profile} takes an interface name")
        return device, {"device": device}
    if len(parts) == 2:
        direction, port = "dst", parts[1]
    elif len(parts) == 3 and parts[1] in {"src", "dst"}:
        direction, port = parts[1:]
    else:
        raise ValueError(f"{profile} takes IFACE:PORT or IFACE:src:PORT")
    if not PORT.fullmatch(port) or not 1 <= int(port) <= 65535:
        raise ValueError("TCP port must be from 1 to 65535")
    port = int(port)
    canonical = f"{device}:{port}" if direction == "dst" else f"{device}:src:{port}"
    return canonical, {"device": device, "direction": direction, "port": port}


def _ip(*args, read=False):
    try:
        result = subprocess.run(["ip", *args], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=5, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError("ip command timed out") from error
    if result.returncode:
        raise ValueError(f"ip {' '.join(args)} failed: {result.stderr.strip()[-400:]}")
    return json.loads(result.stdout) if read else None


def _link(device):
    rows = _ip("-j", "-d", "link", "show", "dev", device, read=True)
    if len(rows) != 1 or rows[0]["ifname"] != device:
        raise ValueError(f"interface {device} did not resolve uniquely")
    return rows[0]


def _identity(row):
    ns = NETNS.stat()
    return {
        "namespace": [ns.st_dev, ns.st_ino],
        "ifindex": row["ifindex"],
        "address": row.get("address"),
        "link": row.get("link_index") or row.get("link"),
        "link_netnsid": row.get("link_netnsid"),
        "kind": row.get("linkinfo", {}).get("info_kind"),
        "mtu": row["mtu"],
    }


def _check_link(row, identity):
    if row.get("ifalias") != TEST_ALIAS or row.get("master") is not None:
        raise ValueError(f"{row['ifname']} must be a dedicated test interface marked with alias {TEST_ALIAS}")
    kind = identity["kind"]
    if kind == "veth":
        return
    if (kind == "ipoib" and row.get("link_type") == "infiniband"
            and identity["link"] not in {None, 0, identity["ifindex"], row["ifname"]}):
        return
    raise ValueError("only dedicated veth or IPoIB child interfaces are supported")


def _current(step):
    row = _link(step["device"])
    identity = _identity(row)
    if identity != step["identity"]:
        raise ValueError("test network namespace or interface identity changed")
    _check_link(row, identity)
    return row


def _routes(device):
    result = []
    for family in ("-4", "-6"):
        for route in _ip("-j", family, "route", "show", "table", "all", read=True):
            if route.get("dev") == device or any(hop.get("dev") == device for hop in route.get("nexthops", [])):
                result.append({**route, "family": family})
    return result


def _check_routes(device):
    routes = _routes(device)
    if any(route.get("dst") in {None, "default", "0.0.0.0/0", "::/0"} for route in routes):
        raise ValueError(f"refusing to inject on {device}: it carries a default route")
    return routes


def _addresses(device):
    rows = _ip("-j", "address", "show", "dev", device, read=True)
    return sorted([info["family"], info["local"], info["prefixlen"]]
                  for row in rows for info in row.get("addr_info", [])
                  if info["family"] != "inet6" or info.get("scope") != "link")


def _ipv4_settings(routes):
    fields = ("dst", "table", "type", "scope", "protocol", "prefsrc", "metric")
    settings = [{key: route[key] for key in fields if key in route}
                for route in routes if route["family"] == "-4"]
    return sorted(settings, key=lambda row: json.dumps(row, sort_keys=True))


def _up(row):
    return "UP" in row["flags"]


def _set_link(step, expected, desired):
    if _up(_current(step)) != expected:
        raise ValueError("test interface admin state changed outside the session")
    if expected != desired:
        _ip("link", "set", "dev", step["device"], "up" if desired else "down")
    if _up(_current(step)) != desired:
        raise ValueError("test interface admin state readback did not match")


def _backend(profile):
    if profile == "tcp-reset":
        from . import ip_tcp_reset

        return ip_tcp_reset
    from . import ip_qdisc

    return ip_qdisc


def prepare(profile, target):
    device = target["device"]
    row = _link(device)
    identity = _identity(row)
    _check_link(row, identity)
    if profile != "tcp-rate" and identity["kind"] != "veth":
        raise ValueError(f"{profile} requires a dedicated test veth interface")
    routes = _check_routes(device)
    ns_device, ns_inode = identity["namespace"]
    step = {"profile": profile, **target, "identity": identity, "mtu": row["mtu"],
            "link_type": row.get("link_type"),
            "resource": f"netdev:{ns_device}:{ns_inode}:{identity['ifindex']}"}
    if not _up(row):
        raise ValueError(f"test interface {device} must be up before injection")
    if profile in LINK_PROFILES:
        addresses = _addresses(device)
        if any(address[0] == "inet6" for address in addresses) or any(
                route.get("protocol") != "kernel" or route.get("gateway") for route in routes):
            raise ValueError("link switching requires connected test routes and no configured IPv6 addresses")
        step.update(admin_up=True, addresses=addresses, ipv4_routes=_ipv4_settings(routes), flap_transitions=0)
    else:
        _backend(profile).prepare(step)
    return step


def observe(step):
    row = _current(step)
    result = {"device": step["device"], "ifindex": row["ifindex"], "admin_up": _up(row)}
    if step["profile"] in LINK_PROFILES:
        return {**result, "addresses": _addresses(step["device"]),
                "ipv4_routes": _ipv4_settings(_routes(step["device"])),
                "flap_transitions": step["flap_transitions"]}
    return {**result, **_backend(step["profile"]).observe(step)}


def apply(step):
    _current(step)
    routes = _check_routes(step["device"])
    if step["profile"] in LINK_PROFILES:
        if _addresses(step["device"]) != step["addresses"] or _ipv4_settings(routes) != step["ipv4_routes"]:
            raise ValueError("test interface addresses or routes changed before injection")
        _set_link(step, step["admin_up"], False)
        return observe(step)
    result = _backend(step["profile"]).apply(step)
    return {"device": step["device"], **result}


def restore(step):
    row = _current(step)
    if step["profile"] in LINK_PROFILES:
        _set_link(step, _up(row), step["admin_up"])
        result = observe(step)
        if result["addresses"] != step["addresses"] or result["ipv4_routes"] != step["ipv4_routes"]:
            raise ValueError("test interface addresses or routes differ from original after restore")
        return result
    return {"device": step["device"], **_backend(step["profile"]).restore(step)}


def tick(step, elapsed):
    if step["profile"] != "rail-flap":
        return False
    desired = int(elapsed // FLAP_SECONDS) % 2 != 0
    expected = step["flap_transitions"] % 2 != 0
    if desired == expected:
        return False
    if not desired:
        _check_routes(step["device"])
    _set_link(step, expected, desired)
    step["flap_transitions"] += 1
    return True
