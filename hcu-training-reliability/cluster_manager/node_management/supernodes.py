# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Physical supernode inventory and placement of complete node groups."""

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path
from string import Formatter


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Pod:
    name: str
    cards_per_node: int
    active: int
    hosts: tuple


def _numbers(expression):
    if not isinstance(expression, str):
        raise ValueError("pod_range must be a string, e.g. '1-4,7'")
    for item in expression.split(","):
        match = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", item.strip())
        if not match:
            raise ValueError(f"Invalid pod_range item: {item!r}")
        start = int(match[1])
        end = int(match[2] or match[1])
        if end < start:
            raise ValueError(f"Descending pod_range: {item}")
        yield from range(start, end + 1)


def _template(value, fields):
    if not isinstance(value, str):
        raise ValueError("Expected a name template")
    found = set()
    for _, field, spec, conversion in Formatter().parse(value):
        if field is None:
            continue
        if field not in fields or conversion or (spec and not re.fullmatch(r"0[1-9][0-9]?d", spec)):
            raise ValueError(f"Unexpected field or format in name template: {value!r}")
        found.add(field)
    if found != fields:
        raise ValueError(f"Template {value!r} must include {sorted(fields)}")
    return value


def _read_rule(rule):
    if not isinstance(rule, dict):
        raise ValueError("Expected a rule object")
    spec = rule.get("spec")
    size = re.fullmatch(r"([1-9][0-9]*)\*([1-9][0-9]*)", spec) if isinstance(spec, str) else None
    if not size:
        raise ValueError(f"Expected a positive M*N spec, got {spec!r}")
    count, cards = map(int, size.groups())
    active = rule.get("active_nodes_per_pod")
    if type(active) is not int or not 1 <= active <= count:
        raise ValueError(f"active_nodes_per_pod must be an integer in 1..{count}")
    standby = rule.get("local_standby_nodes_per_pod", count - active)
    if type(standby) is not int or standby != count - active:
        raise ValueError(f"local_standby_nodes_per_pod must equal {count - active} for this rule")
    start = rule.get("node_start", 0)
    if type(start) is not int or start < 0:
        raise ValueError("node_start must be a non-negative integer")
    pod_template = _template(rule.get("pod_name"), {"pod"})
    host_template = _template(rule.get("node_name"), {"pod", "node"})
    for number in _numbers(rule.get("pod_range")):
        name = pod_template.format(pod=number)
        hosts = tuple(host_template.format(pod=number, node=n) for n in range(start, start + count))
        for identifier in (name, *hosts):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
                raise ValueError(f"Invalid generated name: {identifier!r}")
        yield _Pod(name, cards, active, hosts)


class SupernodeTopology:
    """Expand configured physical membership once; allocations only use supplied candidates."""

    def __init__(self, rules):
        if not isinstance(rules, list) or not rules:
            raise ValueError("supernodes.json requires a non-empty rules list")
        self._pods = {}
        self._owner = {}
        for index, rule in enumerate(rules):
            try:
                for pod in _read_rule(rule):
                    if pod.name in self._pods:
                        raise ValueError(f"Duplicate physical Pod: {pod.name}")
                    self._pods[pod.name] = pod
                    for host in pod.hosts:
                        if host in self._owner:
                            raise ValueError(f"Duplicate compute host: {host}")
                        self._owner[host] = pod.name
            except ValueError as exc:
                raise ValueError(f"supernodes.json rules[{index}]: {exc}") from exc

    @classmethod
    def from_file(cls, path):
        with Path(path).open(encoding="utf-8-sig") as stream:
            config = json.load(stream)
        if not isinstance(config, dict):
            raise ValueError("supernodes.json requires an object with a rules list")
        return cls(config.get("rules"))

    def compute_hosts(self):
        """List every compute host, in configured Pod and node order."""
        return list(self._owner)

    def validate_hosts(self, hosts):
        missing = set(hosts).difference(self._owner)
        if missing:
            raise ValueError(f"Compute hosts missing from supernode rules: {', '.join(sorted(missing))}")

    def pod_hosts_for(self, layout):
        """Expand selected Pods in first-appearance order, including all their standbys."""
        layout = list(layout)
        self.validate_hosts(layout)
        names = dict.fromkeys(self._owner[host] for host in layout)
        return [host for name in names for host in self._pods[name].hosts]

    def validate_layout(self, hosts, slots, *, initial=False):
        """Return rank positions by Pod. Migrated groups may be smaller than the target quota."""
        self.validate_hosts(hosts)
        if len(hosts) != len(set(hosts)):
            raise ValueError("Previous supernode layout contains duplicate hosts")
        positions = defaultdict(list)
        for index, host in enumerate(hosts):
            positions[self._owner[host]].append(index)
        for name, indices in positions.items():
            pod = self._pods[name]
            size = len(indices)
            if size > pod.active or (initial and size != pod.active) or slots > pod.cards_per_node:
                raise ValueError(f"Previous layout incompatible with {name}: "
                                 f"{size} positions, configured {pod.active} x {pod.cards_per_node}")
        return positions

    @staticmethod
    def _choose_initial_pods(pods, count):
        """Choose whole training groups totaling exactly count; never truncate a Pod."""
        if not pods or count > sum(p.active for p in pods):
            return None
        if all(p.active == pods[0].active for p in pods):
            take, remainder = divmod(count, pods[0].active)
            return None if remainder else pods[:take]

        # Mixed group sizes need exact subset selection. Keep one predecessor
        # per reachable node count (bounded by the job size), not all layouts.
        parents = {0: None}
        for pod in pods:
            for previous in list(parents):
                total = previous + pod.active
                if total <= count and total not in parents:
                    parents[total] = (previous, pod)
            if count in parents:
                selected = []
                while count:
                    count, pod = parents[count]
                    selected.append(pod)
                return selected[::-1]
        return None

    def plan(self, count, slots, previous, healthy, degraded, *, initial=False):
        """Plan without mutation. initial=True reserves all standbys for a requested host list."""
        if count <= 0 or slots <= 0:
            raise ValueError("Node count and slots must be positive")
        healthy = set(healthy)
        degraded = set(degraded) - healthy
        previous = list(previous or ())
        self.validate_hosts(healthy | degraded)
        if previous and len(previous) != count:
            raise ValueError("Previous supernode layout must contain every position")
        positions = self.validate_layout(previous, slots, initial=initial)

        tiers = [healthy]
        if degraded:
            tiers.append(healthy | degraded)
        for index, eligible in enumerate(tiers):
            candidates = {}
            for name, pod in self._pods.items():
                if pod.cards_per_node < slots:
                    continue
                hosts = [host for host in pod.hosts if host in eligible]
                required = len(pod.hosts) if initial or not previous else 1
                if len(hosts) >= required:
                    candidates[name] = sorted(hosts, key=lambda host: host not in healthy)
            if previous:
                layout = self._recover(previous, positions, candidates, healthy, initial, index == len(tiers) - 1)
            else:
                full = [self._pods[name] for name in candidates]
                if index:
                    full.sort(key=lambda pod: sum(host not in healthy for host in pod.hosts))
                selected = self._choose_initial_pods(full, count)
                layout = None if selected is None else [
                    host for pod in selected for host in candidates[pod.name][:pod.active]
                ]
                if layout is None and index == len(tiers) - 1:
                    logger.warning("Cannot initially allocate %d nodes: %d compatible full Pods "
                                   "have all configured active and standby nodes available", count, len(full))
            if layout is not None:
                return layout
        return None

    def _recover(self, previous, positions, candidates, healthy, initial, report_shortage):
        layout = list(previous)
        moves = []
        retained_pods = set()
        for name, indices in positions.items():
            if len(candidates.get(name, ())) < len(indices):
                moves.append(name)
                continue
            retained_pods.add(name)
            # Reserve all reusable original hosts before filling holes. Ties
            # retain the configured numeric host order within each health tier.
            originals = {previous[i] for i in indices}
            ranked = sorted(candidates[name], key=lambda h: (h not in healthy, h not in originals))
            selected = ranked[:len(indices)]
            retained = originals.intersection(selected)
            spares = iter(h for h in selected if h not in retained)
            for i in indices:
                if previous[i] not in retained:
                    layout[i] = next(spares)

        if not moves:
            return layout

        # Match the largest groups first. Once a destination can fit this group,
        # it can fit every smaller group with the same physical card width.
        # Vacated Pods can receive a smaller group; retained Pods are reserved.
        destinations = defaultdict(list)
        for order, (name, hosts) in enumerate(candidates.items()):
            if name in retained_pods:
                continue
            pod = self._pods[name]
            capacity = min(pod.active, len(hosts))
            healthy_count = sum(h in healthy for h in hosts[:capacity])
            key = (pod.cards_per_node, pod.active if initial else None)
            destinations[key].append((capacity, -healthy_count, order, name))
        for pool in destinations.values():
            pool.sort()

        ready = defaultdict(list)
        for name in sorted(moves, key=lambda n: len(positions[n]), reverse=True):
            source = self._pods[name]
            size = len(positions[name])
            key = (source.cards_per_node, source.active if initial else None)
            waiting = destinations[key]
            compatible = ready[key]
            while waiting and waiting[-1][0] >= size:
                _, health_priority, order, target = waiting.pop()
                heappush(compatible, (health_priority, order, target))
            if not compatible:
                if report_shortage:
                    logger.warning("Cannot relocate %s: need one free compatible Pod with %d "
                                   "eligible %d-card hosts", name, size, source.cards_per_node)
                return None
            _, _, destination = heappop(compatible)
            for index, host in zip(positions[name], candidates[destination]):
                layout[index] = host
        return layout
