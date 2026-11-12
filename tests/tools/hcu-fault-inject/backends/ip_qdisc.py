# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Traffic controls on an already validated, dedicated test interface."""

import hashlib
import json
import math
import secrets
import shutil
import subprocess


DEFAULT_QUEUES = {"mq", "noqueue", "pfifo_fast", "fq_codel", "pfifo", "bfifo", "fq"}
NETEM = {
    "tcp-loss": (["loss", "100%"], {"loss-random": {"loss": 1.0}}),
    "tcp-drop": (["loss", "100%"], {"loss-random": {"loss": 1.0}}),
    "tcp-jitter": (["delay", "45ms", "25ms"], {"delay": {"delay": 0.045, "jitter": 0.025}}),
    "tcp-reorder": (["delay", "40ms", "reorder", "100%", "gap", "3"],
                    {"delay": {"delay": 0.040}, "reorder": {"reorder": 1.0}}),
    "tcp-random-loss": (["loss", "random", "10%"], {"loss-random": {"loss": 0.10}}),
    "tcp-burst-loss": (["loss", "gemodel", "5%", "33%", "100%", "0%"],
                       {"loss-gemodel": {"p": 0.05, "r": 0.33, "1-h": 1.0, "1-k": 0.0}}),
}
STATS = ("bytes", "packets", "drops", "overlimits", "requeues", "backlog", "qlen",
         "delayed", "dropped", "reordered")


def _tc(step, *args, read=False):
    command = [step["qdisc"]["tc"]]
    if read:
        command.extend(("-j", "-s"))
    try:
        result = subprocess.run([*command, *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False, timeout=5)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"tc timed out on {step['device']}") from error
    if result.returncode:
        detail = result.stderr.strip()[-500:] or f"exit status {result.returncode}"
        raise RuntimeError(f"tc failed on {step['device']}: {detail}")
    if read:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("unexpected tc JSON readback")
        return rows
    return None


def _qdiscs(step):
    return _tc(step, "qdisc", "show", "dev", step["device"], read=True)


def _root(rows):
    if not rows:
        return {"kind": "noqueue", "handle": "0:"}
    roots = [row for row in rows if row.get("root")]
    if len(roots) != 1:
        raise RuntimeError("expected exactly one root qdisc on the test interface")
    return roots[0]


def _fingerprint(rows):
    settings = (
        {key: row[key] for key in ("kind", "handle", "root", "parent", "options") if key in row}
        for row in rows
    )
    encoded = sorted(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in settings)
    return hashlib.sha256("\n".join(encoded).encode()).hexdigest()


def _netem(profile, value):
    if profile == "tcp-delay":
        if value is None:
            value = 80
        if type(value) is not int or not 1 <= value <= 3000:
            raise ValueError("TCP delay must be 1 to 3000 milliseconds")
        return ["delay", f"{value}ms"], {"delay": {"delay": value / 1000}}
    if value is not None:
        raise ValueError("--value is only used by tcp-rate and tcp-delay")
    return NETEM[profile]


def prepare(step):
    program = step.get("tc") or shutil.which("tc")
    if program is None:
        raise RuntimeError("tc from iproute2 is required")
    handle = 0x8000 + secrets.randbelow(0x7000)
    plan = {"tc": program, "handle": f"{handle:x}:"}
    step["qdisc"] = plan
    rows = _qdiscs(step)
    _root(rows)
    if any(row.get("handle") != "0:" or row.get("kind") not in DEFAULT_QUEUES for row in rows):
        raise ValueError("test interface has an explicitly configured qdisc")
    plan["baseline"] = _fingerprint(rows)

    value = step.get("value")
    if step["profile"] == "tcp-rate":
        rate = 40 if value is None else value
        if type(rate) is not int or not 1 <= rate <= 100_000:
            raise ValueError("TCP/IPoIB rate must be 1 to 100000 Mbit/s")
        mtu = step.get("mtu")
        if mtu and step.get("identity", {}).get("kind") == "ipoib" and any(
            row.get("kind") == "fq_codel" and row.get("options", {}).get("quantum") != mtu + 24
            for row in rows
        ):
            raise ValueError("IPoIB default queue does not match current MTU; set MTU before bringing the test child up")
        plan.update(kind="tbf", rate_mbps=rate)
    else:
        if type(step.get("port")) is not int or not 1 <= step["port"] <= 65535:
            raise ValueError("the TCP selector must specify a port from 1 to 65535")
        if step.get("direction") not in {"src", "dst"}:
            raise ValueError("the TCP selector must specify a source or destination port")
        args, options = _netem(step["profile"], value)
        plan.update(kind="prio", child=f"{handle + 1:x}:", band=f"{handle:x}:3",
                    netem_args=args, netem_options=options)


def _filters(step, require_complete):
    plan = step["qdisc"]
    rows = _tc(step, "filter", "show", "dev", step["device"], "parent", plan["handle"], read=True)
    expected_protocols = {"ip", "ipv6"}
    matched = set()
    port_field = f"{step['direction']}_port"
    for row in rows:
        if "options" not in row:  # iproute2 also prints each classifier's header.
            continue
        options = row["options"]
        keys = {key: value for key, value in options.get("keys", {}).items() if key != "eth_type"}
        protocol = row.get("protocol")
        if (row.get("kind") != "flower" or protocol not in expected_protocols or protocol in matched
                or options.get("classid") != plan["band"] or options.get("actions")
                or options.get("indev") or keys != {"ip_proto": "tcp", port_field: step["port"]}):
            raise RuntimeError("unexpected classifier in the session-owned qdisc")
        matched.add(protocol)
    if require_complete and matched != expected_protocols:
        raise RuntimeError("the selected TCP classifier did not read back")


def _owned_child(step, rows, root, require_complete):
    plan = step["qdisc"]
    if root.get("kind") != plan["kind"] or root.get("handle") != plan["handle"]:
        raise RuntimeError("test interface qdisc changed outside this session")
    children = [row for row in rows if row is not root and row.get("handle") != "0:"]
    if plan["kind"] == "tbf":
        if children:
            raise RuntimeError("unexpected qdisc attached below the session-owned TBF")
        return root
    if len(children) > 1 or any(
        row.get("kind") != "netem" or row.get("handle") != plan["child"]
        or row.get("parent") != plan["band"] for row in children
    ):
        raise RuntimeError("unexpected child attached below the session-owned prio qdisc")
    _filters(step, require_complete)
    if require_complete and not children:
        raise RuntimeError("the session-owned netem child did not read back")
    return children[0] if children else None


def _checked_options(step, row):
    plan = step["qdisc"]
    options = row.get("options", {})
    if plan["kind"] == "tbf":
        if options.get("rate") != plan["rate_mbps"] * 125_000:
            raise RuntimeError("configured TCP/IPoIB rate did not read back")
    else:
        for section, fields in plan["netem_options"].items():
            actual = options.get(section, {})
            for key, expected in fields.items():
                found = actual.get(key)
                if not isinstance(found, (int, float)) or not math.isclose(found, expected, abs_tol=0.0001):
                    raise RuntimeError(f"configured netem option {section}.{key} did not read back")
    return options


def observe(step):
    rows = _qdiscs(step)
    root = _root(rows)
    plan = step["qdisc"]
    result = {"device": step["device"], "root": root["kind"], "handle": root["handle"]}
    snapshot = _fingerprint(rows)
    if snapshot == plan["baseline"]:
        return {**result, "active": False, "queue_fingerprint": snapshot}
    child = _owned_child(step, rows, root, require_complete=True)
    options = _checked_options(step, child)
    result.update(active=True, options=options,
                  statistics={key: child[key] for key in STATS if key in child})
    if plan["kind"] == "tbf":
        result["rate_mbps"] = plan["rate_mbps"]
    else:
        result.update(port=step["port"], direction=step["direction"])
    return result


def apply(step):
    if _fingerprint(_qdiscs(step)) != step["qdisc"]["baseline"]:
        raise RuntimeError("test interface qdisc changed after preparation")
    plan = step["qdisc"]
    dev = ["dev", step["device"]]
    root = ["root", "handle", plan["handle"]]
    if plan["kind"] == "tbf":
        rate = plan["rate_mbps"] * 125_000
        burst = max(65536, min(rate // 100, 4 * 1024 * 1024))
        _tc(step, "qdisc", "add", *dev, *root, "tbf", "rate", f"{rate * 8}bit",
            "burst", str(burst), "latency", "50ms")
    else:
        _tc(step, "qdisc", "add", *dev, *root, "prio", "bands", "3", "priomap", *(["0"] * 16))
        _tc(step, "qdisc", "add", *dev, "parent", plan["band"], "handle", plan["child"],
            "netem", *plan["netem_args"])
        for preference, protocol in enumerate(("ip", "ipv6"), 1):
            _tc(step, "filter", "add", *dev, "parent", plan["handle"], "protocol", protocol,
                "pref", str(preference), "flower", "skip_hw", "ip_proto", "tcp",
                f"{step['direction']}_port", str(step["port"]), "classid", plan["band"])
    result = observe(step)
    if not result["active"]:
        raise RuntimeError("test interface qdisc did not take effect")
    return result


def restore(step):
    rows = _qdiscs(step)
    plan = step["qdisc"]
    if _fingerprint(rows) != plan["baseline"]:
        _owned_child(step, rows, _root(rows), require_complete=False)
        _tc(step, "qdisc", "del", "dev", step["device"], "root", "handle", plan["handle"])
    result = observe(step)
    if result["active"]:
        raise RuntimeError("original test interface qdisc was not restored")
    return result
