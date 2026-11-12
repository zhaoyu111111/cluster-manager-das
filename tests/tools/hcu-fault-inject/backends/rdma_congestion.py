# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Bounded RDMA load between two explicitly paired test endpoints."""

import ctypes
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .resource import _CHILDREN, _matches, _require_helper_control, _stat, _stop_helper


PROFILES = ("rdma-congest", "rdma-burst")
LINK = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_.-]{0,62})/([1-9][0-9]{0,2})")
TOKEN = re.compile(r"[0-9a-f]{64}")
TEST_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                               "198.18.0.0/15")))
MAX_SECONDS = 120
DEFAULT_GBPS = 25
MAX_GBPS = 50
CONTROL_TIMEOUT = 6
START_TIMEOUT = 12
READY_SECONDS = 0.4
FRAME_SIZE = 4096
TAG_SIZE = hashlib.sha256().digest_size
HELLO = struct.Struct("!BHB16s")


def _test_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("use a literal IPv4 address on a private test network") from None
    if not any(address in network for network in TEST_NETWORKS):
        raise ValueError("RDMA load requires a private or benchmark IPv4 address")
    return str(address)


def _link(value):
    match = LINK.fullmatch(value)
    if not match or int(match[2]) > 255:
        raise ValueError("RDMA link must be DEVICE/PORT, for example shca0/1")
    return match[1], int(match[2])


def parse_target(target):
    link, sep, endpoint = target.partition("@")
    _link(link)
    parts = endpoint.split(":")
    if not sep or len(parts) != 3 or not re.fullmatch(r"[0-9]{1,5}", parts[1]) or not TOKEN.fullmatch(parts[2]):
        raise ValueError("use LINK@IP:PORT:TOKEN printed by the paired rdma-listen")
    peer, port, secret = _test_ip(parts[0]), int(parts[1]), parts[2]
    if not 1024 <= port <= 65535:
        raise ValueError("RDMA control port must be between 1024 and 65535")
    fingerprint = hashlib.sha256(bytes.fromhex(secret)).hexdigest()[:16]
    return f"{link}@{peer}:{port}#{fingerprint}", {
        "link": link, "peer": peer, "control_port": port, "secret": secret,
    }


def _checked_json(*command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{command[0]} query timed out") from error
    if result.returncode:
        raise RuntimeError(f"{command[0]} query failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _port(link):
    device, port = _link(link)
    path = Path("/sys/class/infiniband") / device / "ports" / str(port)
    if (path / "state").read_text(encoding="ascii").partition(":")[0].strip() != "4":
        raise ValueError(f"RDMA link {link} must be ACTIVE")
    if (path / "link_layer").read_text(encoding="ascii").strip() != "InfiniBand":
        raise ValueError("this RDMA load backend requires a native InfiniBand test port")
    return path


def _route(peer, expected_source=None):
    args = ("from", expected_source) if expected_source else ()
    rows = _checked_json("ip", "-j", "-4", "route", "get", peer, *args)
    if (len(rows) != 1 or rows[0].get("gateway") or not rows[0].get("dev")
            or rows[0].get("type", "unicast") != "unicast"):
        raise ValueError("the paired endpoint must be directly reachable on the isolated test network")
    device = rows[0]["dev"]
    source = _test_ip(expected_source or rows[0].get("prefsrc") or rows[0].get("src") or "")
    if peer == source:
        raise ValueError("paired RDMA control addresses must be distinct")
    links = _checked_json("ip", "-j", "-d", "address", "show")
    selected = next((row for row in links if row.get("ifname") == device), {})
    addresses = [address.get("local") for address in selected.get("addr_info", [])]
    if selected.get("ifalias") != "hcu-fault-inject" or source not in addresses:
        raise ValueError("the control IP must belong to a dedicated interface marked hcu-fault-inject")
    if any(row.get("ifname") != "lo" and row.get("ifalias") != "hcu-fault-inject"
           and ("UP" in row.get("flags", []) or row.get("addr_info")) for row in links):
        raise ValueError("run paired RDMA load in a namespace with only marked test interfaces")
    for family in ("-4", "-6"):
        defaults = _checked_json("ip", "-j", family, "route", "show", "table", "all", "default")
        if any(row.get("dev") or row.get("nexthops") for row in defaults):
            raise ValueError("the paired test network namespace must not carry a default route")
    return source


def _program():
    program = shutil.which("ib_write_bw")
    if not program:
        raise ValueError("ib_write_bw from perftest is required")
    try:
        result = subprocess.run([program, "--help"], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("ib_write_bw feature discovery timed out") from error
    usage = result.stdout + result.stderr
    options = set(re.findall(r"--[a-z_][a-z0-9_-]*", usage, re.IGNORECASE))
    required = ("--rate_limit", "--rate_limit_type")
    if any(flag not in options for flag in required):
        raise ValueError("installed ib_write_bw requires software rate limiting support")
    return program


def _rate(value):
    if type(value) is not int or not 1 <= value <= MAX_GBPS:
        raise ValueError(f"RDMA test load rate must be 1 to {MAX_GBPS} Gbit/s")
    return value


def _seconds(value):
    if type(value) is not int or not 1 <= value <= MAX_SECONDS:
        raise ValueError(f"RDMA test load duration must be 1 to {MAX_SECONDS} seconds")
    return value


def prepare(step):
    _require_helper_control()
    port = _port(step["link"])
    step.update(program=_program(), source=_route(step["peer"]), helper=None,
                rate=_rate(step.get("value", DEFAULT_GBPS)), resource=f"rdma-load:{port.resolve()}",
                namespace=_namespace())


def _namespace():
    return [os.readlink(f"/proc/self/ns/{kind}") for kind in ("pid", "net")]


def _require_namespace(step):
    if _namespace() != step["namespace"]:
        raise ValueError("run RDMA load operations in the original PID and network namespaces")


def _tag(key, data):
    return hmac.digest(key, data, "sha256")


def _exact(stream, size):
    result = bytearray()
    original_timeout = stream.gettimeout()
    deadline = time.monotonic() + CONTROL_TIMEOUT
    try:
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("paired RDMA control frame timed out")
            stream.settimeout(remaining)
            part = stream.recv(size - len(result))
            if not part:
                raise EOFError("paired RDMA control connection closed")
            result.extend(part)
    finally:
        stream.settimeout(original_timeout)
    return bytes(result)


def _verified(stream, key, prefix, size):
    payload = _exact(stream, size)
    if not hmac.compare_digest(_exact(stream, TAG_SIZE), _tag(key, prefix + payload)):
        raise ValueError("paired RDMA endpoint authentication failed")
    return payload


def _authenticate_client(stream, step):
    key = bytes.fromhex(step["secret"])
    nonce = _exact(stream, 16)
    hello = HELLO.pack(PROFILES.index(step["profile"]), step["seconds"], step["rate"], secrets.token_bytes(16))
    stream.sendall(hello + _tag(key, b"client" + nonce + hello))
    size = _verified(stream, key, b"size" + nonce + hello, 1)[0]
    if not 3 <= size <= 67:
        raise ValueError("invalid paired RDMA link identity")
    link = _verified(stream, key, b"server" + nonce + hello, size).decode("ascii")
    _link(link)
    return _tag(key, b"relay" + nonce + hello), link


def _authenticate_server(stream, key):
    nonce = secrets.token_bytes(16)
    stream.sendall(nonce)
    hello = _verified(stream, key, b"client" + nonce, HELLO.size)
    profile, seconds, rate, _ = HELLO.unpack(hello)
    if profile >= len(PROFILES):
        raise ValueError("unknown paired RDMA load profile")
    return nonce, hello, _seconds(seconds), _rate(rate)


def _relay(control, local, key, *, sender, tick):
    incoming = outgoing = 0
    direction = b"C" if sender else b"S"
    reverse = b"S" if sender else b"C"
    while tick():
        for stream in select.select([control, local], [], [], 0.05)[0]:
            if stream is local:
                payload = local.recv(FRAME_SIZE)
                if not payload:
                    return "benchmark control socket closed"
                length = struct.pack("!H", len(payload))
                prefix = direction + outgoing.to_bytes(8, "big") + length
                control.sendall(length + payload + _tag(key, prefix + payload))
                outgoing += 1
            else:
                try:
                    length = _exact(control, 2)
                except EOFError:
                    return "paired control socket closed"
                size = int.from_bytes(length, "big")
                if not 1 <= size <= FRAME_SIZE:
                    raise ValueError("invalid paired RDMA control frame length")
                prefix = reverse + incoming.to_bytes(8, "big") + length
                local.sendall(_verified(control, key, prefix, size))
                incoming += 1
    return "test window ended"


def _stop_signal(_signum, _frame):
    raise KeyboardInterrupt


def _parent_guard(parent, signum):
    if ctypes.CDLL(None, use_errno=True).prctl(1, signum, 0, 0, 0) != 0 or os.getppid() != parent:
        raise RuntimeError("RDMA helper could not bind its lifetime to its parent")


def _compile_loopback(directory):
    compiler = shutil.which("cc")
    if not compiler:
        raise ValueError("rdma-listen requires a C compiler to constrain the perftest listener")
    source = Path(__file__).with_name("rdma_loopback.c")
    library = Path(directory) / "loopback.so"
    try:
        result = subprocess.run([compiler, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
                                 str(source), "-o", str(library), "-ldl"], capture_output=True,
                                text=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("perftest loopback guard compilation timed out") from error
    if result.returncode:
        raise RuntimeError(f"perftest loopback guard compilation failed: {result.stderr[-1200:].strip()}")
    return library


def _benchmark(program, link, port, seconds, rate, output, *, loopback_library=None):
    device, ib_port = _link(link)
    parent = os.getpid()
    argv = [program, "-d", device, "-i", str(ib_port), "-p", str(port), "-D", str(seconds + 2),
            "-f", "0", "-q", "1", "-t", "8", "-Q", "1", "-s", "65536", "-F",
            "--rate_limit", str(rate), "--rate_limit_type", "SW"]
    env = None
    if loopback_library is None:
        argv.append("127.0.0.1")
    else:
        env = dict(os.environ)
        previous = env.get("LD_PRELOAD")
        env["LD_PRELOAD"] = f"{loopback_library}:{previous}" if previous else str(loopback_library)
        env["HCU_RDMA_LOOPBACK_PORT"] = str(port)
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                env=env, preexec_fn=lambda: _parent_guard(parent, signal.SIGKILL))
    except subprocess.SubprocessError as error:
        raise RuntimeError("could not start an owned ib_write_bw process") from error


def _loopback():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(CONTROL_TIMEOUT)
        return listener
    except OSError:
        listener.close()
        raise


def _process_error(process, output):
    output.seek(0)
    text = output.read(8192).decode(errors="replace")
    detail = "\n".join(line for line in text.splitlines() if not re.search(r"\b(rkey|vaddr)\b", line))[-1200:]
    return RuntimeError(f"ib_write_bw exited ({process.poll()}): {detail}")


def _finish(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)


def _owned_loopback_listener(port, pid):
    process = Path(f"/proc/{pid}")
    try:
        inodes = set()
        for fd in (process / "fd").iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            if match := re.fullmatch(r"socket:\[([0-9]+)\]", target):
                inodes.add(match[1])
        entries = (process / "net" / "tcp").read_text(encoding="ascii").splitlines()[1:]
    except FileNotFoundError:
        return False
    for entry in entries:
        fields = entry.split()
        address, number = fields[1].split(":")
        if fields[9] not in inodes or fields[3] != "0A":
            continue
        if (int(number, 16) != port
                or struct.pack("=I", int(address, 16)) != socket.inet_aton("127.0.0.1")):
            raise RuntimeError("ib_write_bw did not bind only the expected loopback listener")
        return True
    return False


def _connect_local(port, process, output):
    deadline = time.monotonic() + CONTROL_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise _process_error(process, output)
        if not _owned_loopback_listener(port, process.pid):
            time.sleep(0.02)
            continue
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=0.2)
        except ConnectionRefusedError:
            time.sleep(0.02)
    raise RuntimeError("paired perftest receiver did not open its loopback control socket")


def listen(link, bind, peer):
    """Accept one authenticated test peer, then release the receiver."""
    _port(link)
    program = _program()
    bind, peer = _test_ip(bind), _test_ip(peer)
    _route(peer, bind)
    signal.signal(signal.SIGTERM, _stop_signal)
    key = secrets.token_bytes(32)
    try:
        with tempfile.TemporaryDirectory(prefix="hcu-rdma-loopback-") as directory:
            library = _compile_loopback(directory)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind((bind, 0))
                listener.listen(1)
                listener.settimeout(60)
                print(f"{bind}:{listener.getsockname()[1]}:{key.hex()}", flush=True)
                control, address = listener.accept()
            with control:
                if address[0] != peer:
                    raise ValueError("connection source does not match the declared RDMA test peer")
                control.settimeout(CONTROL_TIMEOUT)
                nonce, hello, seconds, rate = _authenticate_server(control, key)
                _port(link)
                _route(peer, bind)
                with _loopback() as reservation:
                    local_port = reservation.getsockname()[1]
                with tempfile.TemporaryFile() as output:
                    benchmark = _benchmark(program, link, local_port, seconds, rate, output,
                                           loopback_library=library)
                    try:
                        with _connect_local(local_port, benchmark, output) as local:
                            local.settimeout(CONTROL_TIMEOUT)
                            identity = link.encode("ascii")
                            size = bytes([len(identity)])
                            control.sendall(size + _tag(key, b"size" + nonce + hello + size)
                                            + identity + _tag(key, b"server" + nonce + hello + identity))
                            relay_key = _tag(key, b"relay" + nonce + hello)
                            deadline = time.monotonic() + seconds + START_TIMEOUT
                            reason = _relay(control, local, relay_key, sender=False,
                                            tick=lambda: time.monotonic() < deadline and benchmark.poll() is None)
                            if reason == "benchmark control socket closed" and benchmark.poll() is None:
                                try:
                                    benchmark.wait(timeout=0.3)
                                except subprocess.TimeoutExpired:
                                    pass
                            if benchmark.poll() not in (None, 0):
                                raise _process_error(benchmark, output)
                    finally:
                        _finish(benchmark)
    except KeyboardInterrupt:
        return
    except TimeoutError as error:
        raise RuntimeError("paired RDMA listener or control connection timed out") from error


def _ready_qps(pid, link):
    device, port = _link(link)
    rows = _checked_json("rdma", "-j", "resource", "show", "qp", "link", link, "pid", str(pid))
    return [int(row["lqpn"]) for row in rows
            if row.get("pid") == pid and row.get("ifname") == device and row.get("port") == port
            and row.get("state") == "RTS" and row.get("type") == "RC"]


def _worker(step):
    _parent_guard(step["parent"], signal.SIGTERM)
    signal.signal(signal.SIGTERM, _stop_signal)
    _require_namespace(step)
    _port(step["link"])
    source = _route(step["peer"], step["source"])
    endpoint = (step["peer"], step["control_port"])
    with socket.create_connection(endpoint, timeout=CONTROL_TIMEOUT, source_address=(source, 0)) as control:
        relay_key, peer_link = _authenticate_client(control, step)
        with _loopback() as listener, tempfile.TemporaryFile() as output:
            listener.listen(1)
            benchmark = _benchmark(step["program"], step["link"], listener.getsockname()[1],
                                   step["seconds"], step["rate"], output)
            try:
                local, _ = listener.accept()
                with local:
                    local.settimeout(CONTROL_TIMEOUT)
                    startup_deadline = time.monotonic() + START_TIMEOUT
                    started = None
                    candidate_qpn = None
                    candidate_since = 0
                    next_query = 0
                    paused = False

                    def tick():
                        nonlocal started, candidate_qpn, candidate_since, next_query, paused
                        now = time.monotonic()
                        if benchmark.poll() is not None:
                            if started is None or benchmark.returncode:
                                raise _process_error(benchmark, output)
                            return False
                        if started is None and now >= next_query:
                            if now >= startup_deadline:
                                raise RuntimeError("could not confirm a perftest-owned RTS QP on the selected link")
                            next_query = now + 0.2
                            qps = _ready_qps(benchmark.pid, step["link"])
                            if len(qps) > 1:
                                raise RuntimeError("expected one perftest-owned QP on the selected link")
                            qpn = qps[0] if qps else None
                            if qpn != candidate_qpn:
                                candidate_qpn, candidate_since = qpn, now
                            elif qpn is not None and now - candidate_since >= READY_SECONDS:
                                started = now
                                print(json.dumps({"ready": True, "peer_link": peer_link, "qpn": qpn}), flush=True)
                        if started is None:
                            return True
                        elapsed = now - started
                        if elapsed >= step["seconds"]:
                            return False
                        if step["profile"] == "rdma-burst":
                            off_window = int(elapsed) % 2 == 1
                            if off_window != paused:
                                benchmark.send_signal(signal.SIGSTOP if off_window else signal.SIGCONT)
                                paused = off_window
                        return True

                    reason = _relay(control, local, relay_key, sender=True, tick=tick)
                    if started is None:
                        try:
                            benchmark.wait(timeout=0.3)
                        except subprocess.TimeoutExpired:
                            _finish(benchmark)
                        raise RuntimeError(f"{reason} before stable QP readiness: {_process_error(benchmark, output)}")
            finally:
                _finish(benchmark)


def apply(step):
    _require_namespace(step)
    _seconds(step["seconds"])
    _port(step["link"])
    _route(step["peer"], step["source"])
    secret = step.pop("secret")
    process = subprocess.Popen([sys.executable, "-B", "-m", "backends.rdma_congestion", "_worker"],
                               cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE)
    try:
        _, start, _ = _stat(process.pid)
        step["helper"] = {"pid": process.pid, "start": start}
        _CHILDREN[process.pid] = process
        with process.stdin:
            process.stdin.write(json.dumps({**step, "secret": secret, "parent": os.getpid()}).encode() + b"\n")
        with process.stdout:
            ready = select.select([process.stdout], [], [], 2 * CONTROL_TIMEOUT + START_TIMEOUT)[0]
            payload = process.stdout.readline(4096) if ready else b""
        receipt = json.loads(payload) if payload else {}
        if receipt.get("ready") is not True or process.poll() is not None:
            raise RuntimeError(receipt.get("error", "paired RDMA test load did not become ready"))
        step["helper"].update(qpn=receipt["qpn"], peer_link=receipt["peer_link"])
        return observe(step)
    except Exception:
        if step["helper"] is not None:
            _stop_helper(step["helper"])
        else:
            _finish(process)
        raise


def observe(step):
    _require_namespace(step)
    helper = step["helper"]
    result = {"link": step["link"], "peer": step["peer"], "limit_gbps": step["rate"], "load_active": False}
    device, port = _link(step["link"])
    counters = Path("/sys/class/infiniband") / device / "ports" / str(port) / "counters"
    port_data = {}
    for name in ("port_xmit_data", "port_rcv_data"):
        try:
            port_data[name] = int((counters / name).read_text(encoding="ascii"))
        except (OSError, ValueError):
            continue
    if port_data:
        result["port_counters"] = port_data
    if helper is not None:
        result.update(load_active=_matches(helper["pid"], helper["start"]), helper_pid=helper["pid"])
        for field in ("qpn", "peer_link"):
            if field in helper:
                result[field] = helper[field]
    return result


def restore(step):
    _require_namespace(step)
    step.pop("secret", None)
    if step["helper"] is not None:
        _stop_helper(step["helper"])
    result = observe(step)
    if result["load_active"]:
        raise RuntimeError("RDMA load helper is still running after restore")
    return result


def tick(step, elapsed):
    helper = step["helper"]
    if elapsed < step["seconds"] - 0.5 and not _matches(helper["pid"], helper["start"]):
        raise RuntimeError("paired RDMA test load ended before the requested window")
    return False


if __name__ == "__main__" and sys.argv[1:] == ["_worker"]:
    try:
        _worker(json.loads(sys.stdin.buffer.readline(8192)))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, KeyError, EOFError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr, flush=True)
        try:
            print(json.dumps({"error": str(error)}), flush=True)
        except BrokenPipeError:
            pass
