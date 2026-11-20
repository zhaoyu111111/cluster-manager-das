# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Reset one established TCP socket on the selected test interface."""

import ipaddress
import json
import os
import socket
import struct
import subprocess


# Linux UAPI from netlink.h, sock_diag.h and inet_diag.h.
_NETLINK_SOCK_DIAG = 4
_SOCK_DESTROY = 21
_NLM_F_REQUEST = 0x01
_NLM_F_ACK = 0x04
_NLMSG_ERROR = 2
_NL_HEADER = struct.Struct("=IHHII")
_NL_ERROR = struct.Struct("=i")
_COOKIE_WORD_MASK = (1 << 32) - 1


def _run(*args):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=5,
                                env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{args[0]} timed out") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{args[0]} failed: {detail or result.returncode}")
    return result.stdout


def _addresses(device):
    links = json.loads(_run("ip", "-j", "address", "show", "dev", device))
    if len(links) != 1 or links[0].get("ifname") != device:
        raise ValueError(f"cannot read addresses for test interface {device}")
    return {info["local"] for info in links[0].get("addr_info", [])
            if info.get("family") in {"inet", "inet6"} and "local" in info}


def _endpoint(value):
    host, _, port = value.rpartition(":")
    address, _, device = host.partition("%")
    address = ipaddress.ip_address(address.strip("[]"))
    port = int(port)
    if (address.is_unspecified or address.is_loopback or address.is_multicast
            or (address.version == 6 and address.is_link_local) or not 1 <= port <= 65535):
        raise ValueError("unsupported TCP endpoint")
    return str(address), port, device.rstrip("]")


def _sockets(family, filters=()):
    flags = ["-H", "-O", "-n", "-t", "-e", f"-{family}"]
    rows = []
    for line in _run("ss", *flags, "state", "established", *filters).splitlines():
        fields = line.split()
        if fields[:1] == ["ESTAB"]:
            fields = fields[1:]
        if len(fields) < 5 or not all(field.isdecimal() for field in fields[:2]):
            continue
        try:
            local, local_port, device = _endpoint(fields[2])
            peer, peer_port, _ = _endpoint(fields[3])
            cookie = next(int(field[3:], 16) for field in fields[4:] if field.startswith("sk:"))
        except (ValueError, StopIteration):
            continue
        if cookie:
            rows.append({"family": family, "local_address": local, "local_port": local_port,
                         "peer_address": peer, "peer_port": peer_port,
                         "bound_device": device, "cookie": cookie})
    return rows


def _route_address(value):
    address = ipaddress.ip_address(value)
    return getattr(address, "ipv4_mapped", None) or address


def _verify_route(device, connection):
    local = _route_address(connection["local_address"])
    peer = _route_address(connection["peer_address"])
    if str(local) not in _addresses(device) or local.version != peer.version:
        raise ValueError(f"TCP socket does not have a local address on {device}")
    route = json.loads(_run("ip", "-j", f"-{local.version}", "route", "get", str(peer), "from", str(local)))
    if len(route) != 1 or route[0].get("dev") != device:
        raise ValueError(f"TCP socket does not use test interface {device}")


def _filters(connection):
    local = connection["local_address"]
    peer = connection["peer_address"]
    if connection["family"] == 6:
        local, peer = f"[{local}]", f"[{peer}]"
    filters = ["src", f"{local}:{connection['local_port']}",
               "and", "dst", f"{peer}:{connection['peer_port']}"]
    if connection["bound_device"]:
        filters.extend(("and", "dev", connection["bound_device"]))
    return filters


def _destroy(connection):
    local = ipaddress.ip_address(connection["local_address"])
    peer = ipaddress.ip_address(connection["peer_address"])
    if local.version != peer.version or local.version != connection["family"]:
        raise ValueError("TCP socket address family changed")
    family = socket.AF_INET if local.version == 4 else socket.AF_INET6
    bound = connection["bound_device"]
    ifindex = socket.if_nametoindex(bound) if bound else 0
    cookie = connection["cookie"]
    if not 0 < cookie < (1 << 64) - 1:
        raise ValueError("TCP socket has no usable kernel identity")

    # The cookie binds this request to the original socket even if a peer
    # has already opened another connection with the same address and ports.
    sockid = (
        struct.pack("!HH", connection["local_port"], connection["peer_port"])
        + local.packed.ljust(16, b"\0")
        + peer.packed.ljust(16, b"\0")
        + struct.pack("=III", ifindex, cookie & _COOKIE_WORD_MASK, cookie >> 32)
    )
    request = struct.pack("=BBBBI", family, socket.IPPROTO_TCP, 0, 0, 0) + sockid
    sequence = 1
    header = _NL_HEADER.pack(_NL_HEADER.size + len(request), _SOCK_DESTROY,
                             _NLM_F_REQUEST | _NLM_F_ACK, sequence, 0)
    try:
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG) as channel:
            channel.settimeout(5)
            channel.bind((0, 0))
            channel.sendto(header + request, (0, 0))
            reply, sender = channel.recvfrom(4096)
    except TimeoutError as error:
        raise RuntimeError("timed out waiting for the kernel TCP reset result") from error

    minimum = 2 * _NL_HEADER.size + _NL_ERROR.size
    if sender != (0, 0) or len(reply) < minimum:
        raise RuntimeError("unexpected kernel TCP reset reply")
    length, kind, _, received_sequence, _ = _NL_HEADER.unpack_from(reply)
    if not minimum <= length <= len(reply) or kind != _NLMSG_ERROR or received_sequence != sequence:
        raise RuntimeError("unexpected kernel TCP reset reply")
    (status,) = _NL_ERROR.unpack_from(reply, _NL_HEADER.size)
    if status:
        raise RuntimeError(f"kernel refused to reset the selected TCP socket: {os.strerror(abs(status))}")


def prepare(step):
    addresses = _addresses(step["device"])
    port_key = "local_port" if step["direction"] == "src" else "peer_port"
    matches = [row for family in (4, 6) for row in _sockets(family)
               if row[port_key] == step["port"]
               and str(_route_address(row["local_address"])) in addresses
               and row["bound_device"] in {"", step["device"]}]
    if len(matches) != 1:
        raise ValueError(f"expected one established TCP socket on {step['device']} "
                         f"with {step['direction']} port {step['port']}; found {len(matches)}")
    connection = matches[0]
    _verify_route(step["device"], connection)
    step.update(connection=connection, reset_performed=False)


def observe(step):
    connection = step["connection"]
    return {
        "mode": "one-shot",
        "connection": {key: value for key, value in connection.items() if key != "cookie"},
        "original_socket_present": connection in _sockets(connection["family"], _filters(connection)),
        "reset_performed": step["reset_performed"],
        "rule_active": False,
    }


def apply(step):
    connection = step["connection"]
    _verify_route(step["device"], connection)
    filters = _filters(connection)
    family = connection["family"]
    if _sockets(family, filters) != [connection]:
        raise ValueError("selected TCP socket changed before reset")
    _destroy(connection)
    step["reset_performed"] = True
    result = observe(step)
    if result["original_socket_present"]:
        raise RuntimeError("selected TCP socket remains established after reset")
    return result


def restore(step):
    # The kernel reset is one-shot. A replacement connection is independent.
    return observe(step)
