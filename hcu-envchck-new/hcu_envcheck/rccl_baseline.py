# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：加载并校验 RCCL 基准配置，根据明确指定的 profile 和测试规模选择基准。
# 职责边界：为报告提供公共基准及来源信息，不自动推断超节点拓扑或调度测试。
"""控制端最终报告使用的严格 INI 基准解析；不读取目标环境或生成执行计划。"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from decimal import Decimal
import math
import re


COLLECTIVES = (
    "all_reduce", "all_gather", "broadcast", "reduce", "reduce_scatter",
    "gather", "scatter", "alltoall", "alltoallv", "sendrecv",
)
_DEFAULT_KEYS = frozenset({"format_version", "error_pct"})
_COMMON_KEYS = frozenset({"kind", "gpu_model", "gpus_per_node", "error_pct"})
_SUPERNODE_KEYS = frozenset({"supernode_capacity_gpus", "nodes_per_supernode"})
_OTHER_KEYS = frozenset({"node_count"})
_PROFILE_KEYS = _COMMON_KEYS | _SUPERNODE_KEYS | _OTHER_KEYS | frozenset(COLLECTIVES)
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_INTEGER = re.compile(r"[0-9]+\Z", re.ASCII)


# 功能：表示基准配置缺失、格式错误或与测试规模不匹配。
class BaselineConfigError(ValueError):
    """The v2 configuration or selected deployment is invalid."""


# 功能：保存一个测试项在指定总卡数下的带宽基准和允许误差。
@dataclass(frozen=True)
class BaselineRow:
    np: int
    test: str
    baseline: Decimal
    error_pct: Decimal


# 功能：读取必填配置字段，拒绝缺失或空值。
def _required(values: configparser.SectionProxy, key: str, section: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise BaselineConfigError(f"[{section}] requires {key}")
    if any(character in value for character in "\r\n\t"):
        raise BaselineConfigError(f"[{section}] {key} must be a single value")
    return value


# 功能：解析有限十进制数，并校验数值范围及后续计算可用性。
def _decimal(value: str, label: str, *, percentage: bool = False) -> Decimal:
    if not _DECIMAL.fullmatch(value):
        raise BaselineConfigError(f"{label} must be a finite decimal number")
    number = Decimal(value)
    # Report metrics use finite floats. Reject overflow/underflow here rather
    # than accepting a reference the existing reporter cannot represent.
    floating = float(number)
    if not math.isfinite(floating):
        raise BaselineConfigError(f"{label} must be a finite decimal number")
    if percentage:
        if not Decimal(0) <= number < Decimal(100):
            raise BaselineConfigError(f"{label} must be in [0, 100)")
    elif number <= 0 or floating <= 0:
        raise BaselineConfigError(f"{label} must be greater than zero")
    return number


# 功能：解析并校验正整数配置值。
def _integer(value: str, label: str) -> int:
    if not _INTEGER.fullmatch(value) or int(value) <= 0:
        raise BaselineConfigError(f"{label} must be a positive integer")
    return int(value)


# 功能：解析逗号分隔的正整数列表。
def _integers(value: str, label: str) -> tuple[int, ...]:
    return tuple(_integer(part.strip(), label) for part in value.split(","))


# 功能：校验实际节点数或每节点卡数为正整数。
def _runtime_count(value: int, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise BaselineConfigError(f"{label} must be a positive integer")


# 功能：按严格 INI 规则读取配置，拒绝重复或无效结构。
def _configuration(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(
        interpolation=None,
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=("#", ";"),
        strict=True,
        empty_lines_in_values=False,
        default_section="__reserved_configparser_defaults__",
    )
    parser.optionxform = str
    try:
        parser.read_string(text.removeprefix("\ufeff"), source="stdin")
    except configparser.Error as exc:
        raise BaselineConfigError(f"invalid INI configuration: {exc}") from exc
    if parser.defaults() or "DEFAULT" in parser:
        raise BaselineConfigError("INI DEFAULT inheritance is not supported")
    if not parser.has_section("defaults"):
        raise BaselineConfigError("configuration requires [defaults]")
    for section in parser.sections():
        if not section.strip() or any(character in section for character in "\r\n\t"):
            raise BaselineConfigError("invalid profile name")
        allowed = _DEFAULT_KEYS if section == "defaults" else _PROFILE_KEYS
        unknown = set(parser[section]) - allowed
        if unknown:
            raise BaselineConfigError(
                f"[{section}] unknown fields: {', '.join(sorted(unknown))}"
            )
    return parser


# 功能：加载指定 profile，校验布局和规模并取得完整测试项基准，允许其他分区尚未标定。
def load_baseline(
    text: str, *, profile: str, nodes: int, gpus_per_node: int,
) -> tuple[BaselineRow, ...]:
    """Validate one named profile and return ten canonical, compatible rows.

    Unselected profiles can contain TBD measurements for future hardware.
    Repeated sections/keys and unknown fields remain configuration errors.
    """
    if not isinstance(profile, str) or not profile.strip() or profile == "defaults":
        raise BaselineConfigError("an explicit nonempty baseline profile is required")
    _runtime_count(nodes, "nodes")
    _runtime_count(gpus_per_node, "gpus_per_node")
    parser = _configuration(text)
    defaults = parser["defaults"]
    if _required(defaults, "format_version", "defaults") != "2":
        raise BaselineConfigError("[defaults] format_version must be 2")
    default_error = _decimal(
        _required(defaults, "error_pct", "defaults"),
        "[defaults] error_pct", percentage=True,
    )
    if not parser.has_section(profile):
        raise BaselineConfigError(f"baseline profile not found: {profile}")
    selected = parser[profile]
    kind = _required(selected, "kind", profile)
    if kind not in {"supernode_hyswitch", "other"}:
        raise BaselineConfigError(f"[{profile}] unsupported kind: {kind}")
    model = _required(selected, "gpu_model", profile)
    if model.upper() == "TBD":
        raise BaselineConfigError(f"[{profile}] gpu_model must be explicitly declared")
    configured_c = _integer(
        _required(selected, "gpus_per_node", profile), f"[{profile}] gpus_per_node"
    )
    if configured_c != gpus_per_node:
        raise BaselineConfigError(
            f"[{profile}] gpus_per_node={configured_c} does not match requested {gpus_per_node}"
        )
    if kind == "supernode_hyswitch":
        if _OTHER_KEYS & set(selected):
            raise BaselineConfigError(f"[{profile}] node_count is not a supernode field")
        capacities = _integers(
            _required(selected, "supernode_capacity_gpus", profile),
            f"[{profile}] supernode_capacity_gpus",
        )
        distribution = _integers(
            _required(selected, "nodes_per_supernode", profile),
            f"[{profile}] nodes_per_supernode",
        )
        if len(capacities) != len(distribution):
            raise BaselineConfigError(
                f"[{profile}] supernode capacity and node distribution lists must have equal lengths"
            )
        for index, (capacity, count) in enumerate(zip(capacities, distribution), start=1):
            if count * configured_c > capacity:
                raise BaselineConfigError(
                    f"[{profile}] supernode {index}: participating GPUs exceed capacity"
                )
        configured_nodes = sum(distribution)
    else:
        if _SUPERNODE_KEYS & set(selected):
            raise BaselineConfigError(f"[{profile}] supernode fields are not valid for kind=other")
        configured_nodes = _integer(
            _required(selected, "node_count", profile), f"[{profile}] node_count"
        )
    if configured_nodes != nodes:
        raise BaselineConfigError(
            f"[{profile}] node count={configured_nodes} does not match requested {nodes}"
        )
    error_pct = default_error
    if "error_pct" in selected:
        error_pct = _decimal(
            _required(selected, "error_pct", profile), f"[{profile}] error_pct", percentage=True
        )
    missing = [test for test in COLLECTIVES if test not in selected]
    if missing:
        raise BaselineConfigError(f"[{profile}] missing baseline tests: {', '.join(missing)}")
    rows = []
    for test in COLLECTIVES:
        value = _required(selected, test, profile)
        if value.upper() == "TBD":
            raise BaselineConfigError(f"[{profile}] {test} has no measured baseline (TBD)")
        rows.append(BaselineRow(
            np=nodes * gpus_per_node,
            test=test,
            baseline=_decimal(value, f"[{profile}] {test}"),
            error_pct=error_pct,
        ))
    return tuple(rows)
