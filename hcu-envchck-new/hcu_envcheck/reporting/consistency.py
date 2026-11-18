# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：归并节点静态配置，保留缺失与差异，不把未知当一致。
# 来源：原 hcu-envcheck/reporting/consistency.py；算法按原实现迁移。
# 文件功能：比较节点环境和资源的静态配置，形成一致性汇总及差异报告。
# 职责边界：仅使用已采集的数据；缺失数据单独标记，不能作为配置相同的依据。
"""Post-processing for node configuration and result consistency."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable

from hcu_envcheck.environment import static_configuration_sections


_NATURAL_PART = re.compile(r"(\d+)")
_VERSION_FIELDS = (("driver_version", "HCU 驱动"), ("dtk_version", "DTK"), ("torch_version", "Torch"))
_VERSION_FIELD_NAMES = {field for field, _ in _VERSION_FIELDS}


def _version_values(record: dict[str, Any]) -> dict[str, Any]:
    """读取版本主项，保留采集值；空值作为缺失信息。"""
    environment = record.get("environment") or {}
    return {field: environment.get(field) or None for field, _ in _VERSION_FIELDS}


def _collection_complete(record: dict[str, Any]) -> bool:
    """采集完成依据执行与解析结果，与后续健康告警分开。"""
    return record.get("execution_status") == "PASS" and record.get("reachable") is True


def _cell(value: Any) -> str:
    text = "未采集" if value is None else (
        json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value))
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("*", "\\*").replace("`", "\\`"))


# 功能：将名称中的数字转换为数值排序键，使节点顺序符合编号顺序。
def _natural_key(value: str) -> list[tuple[int, object]]:
    return [
        (1, int(part)) if part.isdigit() else (0, part.lower())
        for part in _NATURAL_PART.split(value)
        if part
    ]


# 功能：提取环境一致性比较所需的静态字段。
def _environment_view(record: dict[str, Any]) -> dict[str, Any]:
    sections = static_configuration_sections(record)
    return {field: value for section in sections.values() for field, value in section.items()
            if field not in {"mem_total", "dev_shm"}}


# 功能：识别配置数据缺失，生成覆盖不足的说明。
def _missing_configuration(record: dict[str, Any]) -> list[str]:
    if record.get("reachable") is False:
        return ["node_unreachable"]
    environment = record.get("environment") or {}
    missing = []
    for field in ("container_os", "kernel", "driver_version", "dtk_version", "python_version", "python_packages"):
        if field not in environment or environment[field] is None:
            missing.append("platform." + field)
    for field, _ in _VERSION_FIELDS:
        if not environment.get(field) and "platform." + field not in missing:
            missing.append("platform." + field)
    if "hcu_devices" in environment and environment.get("hcu_hardware_status") != "COLLECTED":
        missing.append("platform.hcu_devices")
    for field in ("transformer_engine_version", "deepep_version"):
        if field in environment and not environment[field]:
            missing.append("platform." + field)
    return missing


# 功能：归并相同配置并与参考节点比较，汇总差异、缺失及一致性状态。
def build_consistency_summary(
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(list(records), key=lambda item: _natural_key(str(item.get("node", ""))))
    groups: dict[str, dict[str, Any]] = {}
    missing_by_node: dict[str, list[str]] = {}
    for record in ordered:
        node = str(record.get("node", ""))
        missing = _missing_configuration(record)
        if missing:
            missing_by_node[node] = missing
        configuration = {"platform": _environment_view(record)}
        key = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        group = groups.setdefault(
            key,
            {
                "configuration": configuration,
                "nodes": [],
                "passed_nodes": [],
                "failed_nodes": [],
                "incomplete_nodes": [],
            },
        )
        group["nodes"].append(node)
        status = record.get("status")
        if status == "READY":
            group["passed_nodes"].append(node)
        elif status == "BLOCKED":
            group["failed_nodes"].append(node)
        else:
            group["incomplete_nodes"].append(node)

    configuration_groups = list(groups.values())
    for group in configuration_groups:
        for key in ("nodes", "passed_nodes", "failed_nodes", "incomplete_nodes"):
            group[key].sort(key=_natural_key)
        group["node_count"] = len(group["nodes"])
        group["passed_count"] = len(group["passed_nodes"])
        group["failed_count"] = len(group["failed_nodes"])
        group["incomplete_count"] = len(group["incomplete_nodes"])
        group["partial"] = bool(group["failed_nodes"] or group["incomplete_nodes"])
        group["configuration_evidence"] = "PARTIAL" if any(node in missing_by_node for node in group["nodes"]) else "COLLECTED"
    configuration_groups.sort(key=lambda item: _natural_key(item["nodes"][0]))

    status_counts = Counter(str(record.get("status", "UNKNOWN")) for record in ordered)
    reference_node = next((record["node"] for record in ordered
                           if _collection_complete(record) and all(_version_values(record).values())), None)
    reference = next((group["configuration"] for group in configuration_groups if reference_node in group["nodes"]), {})
    differences: list[dict[str, Any]] = []
    for group in (configuration_groups if reference_node is not None else []):
        if group["configuration"] == reference:
            continue
        changed: dict[str, Any] = {}
        for section in ("platform",):
            current = group["configuration"].get(section, {})
            baseline = reference.get(section, {})
            section_changes = {
                field: {"reference": baseline.get(field), "actual": current.get(field)}
                for field in sorted(set(current) | set(baseline))
                if current.get(field) != baseline.get(field)
                and (field not in _VERSION_FIELD_NAMES or (
                    current.get(field) not in (None, "") and baseline.get(field) not in (None, "")))
            }
            if section_changes:
                changed[section] = section_changes
        if changed:
            differences.append({"nodes": group["nodes"], "changes": changed})

    return {
        "schema_version": "1.0",
        "scope": "platform-only",
        "comparison_status": "DIFFERENT" if differences else ("UNVERIFIED" if reference_node is None or missing_by_node or len(ordered) < 2 else "SAME_OBSERVED_CONFIGURATION"),
        "reference_node": reference_node,
        "configuration_missing_by_node": missing_by_node,
        "configuration_unknown_nodes": list(missing_by_node),
        "comparison_note": "Groups compare collected static values excluding MemTotal and /dev/shm capacity; original values remain in platform-nodes.json. Missing values are not evidence of equality.",
        "node_count": len(ordered),
        "configuration_group_count": len(configuration_groups),
        "status_counts": dict(sorted(status_counts.items())),
        "passed_node_count": sum(status == "READY" for status in (record.get("status") for record in ordered)),
        "failed_node_count": sum(status == "BLOCKED" for status in (record.get("status") for record in ordered)),
        "incomplete_node_count": sum(record.get("status") not in {"READY", "BLOCKED"} for record in ordered),
        "configuration_groups": configuration_groups,
        "differences_from_reference": differences,
    }


_CHECK_LABELS = {
    "PAYLOAD_PARSE": "采集完成", "DTK_VERSION": "DTK版本可读", "HIP_COMPILER": "HIP编译器可用",
    "DRIVER_VERSION": "驱动版本可读", "HYCU_DRIVER_MODULE": "驱动加载", "HCU_DEVICE_NODE": "设备节点可见",
    "RCCL_LIBRARY": "RCCL库可用", "UCX": "UCX可用", "TORCH_IMPORT": "Torch导入",
    "TORCH_HIP_BUILD": "Torch支持HIP", "TORCH_HCU_AVAILABLE": "Torch识卡", "TORCH_DEVICE_COUNT": "可见卡数",
    "NETWORK_HOST_SCOPE": "宿主网络可见", "RDMA_DEVICE_COUNT": "RDMA枚举",
    "RDMA_ACTIVE_DEVICE_COUNT": "RDMA活动设备", "RDMA_CURRENT_PROTOCOL": "RDMA协议",
    "RDMA_EXPECTED_PROTOCOL": "预期RDMA协议", "RDMA_USERSPACE": "RDMA用户态",
    "IB_ENDPOINT": "IB端点", "IB_COUNTER_HEALTH": "IB计数器", "ROCE_ENDPOINT": "RoCE端点",
    "ROCE_COUNTER_HEALTH": "RoCE计数器", "ROCE_DCB_CONFIGURATION": "RoCE DCB", "ROCE_CONFIGURATION_CHAIN": "RoCE配置链",
}
_SOFTWARE_FIELDS = _VERSION_FIELDS + (("transformer_engine_version", "Transformer Engine"), ("deepep_version", "DeepEP"))
_CONDITIONAL_CHECKS = {"NETWORK_HOST_SCOPE", "RDMA_EXPECTED_PROTOCOL", "ROCE_DCB_CONFIGURATION", "ROCE_CONFIGURATION_CHAIN"}
_STATUS_ORDER = {"FAIL": 0, "UNKNOWN": 1, "WARN": 2, "PASS": 3, "NOT_APPLICABLE": 4}


def _nodes(nodes: list[str]) -> str:
    from ClusterShell.NodeSet import NodeSet
    return str(NodeSet.fromlist(nodes))


def _overall(values: Iterable[str]) -> str:
    return min(values, key=lambda value: _STATUS_ORDER[value], default="UNKNOWN")


def _group_rows(rows: Iterable[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in rows:
        key = json.dumps(row["values"], ensure_ascii=False, sort_keys=True)
        groups.setdefault(key, dict(nodes=[], values=row["values"]))["nodes"].append(row["node"])
    return [dict(group, node_count=len(group["nodes"])) for group in groups.values()]


def build_platform_overview(records: Iterable[dict], consistency: dict) -> dict:
    """状态按完整状态向量归并；硬件和软件信息分别归并，保留所有成员。"""
    from hcu_envcheck.environment import canonical_static_value
    records = sorted(records, key=lambda record: _natural_key(record["node"]))
    collected = [record for record in records if _collection_complete(record)]
    check_ids = {row["check_id"] for record in collected for row in record.get("checks", [])}
    if not check_ids:
        check_ids = {"PAYLOAD_PARSE", "HYCU_DRIVER_MODULE", "HCU_DEVICE_NODE", "TORCH_HCU_AVAILABLE", "RDMA_USERSPACE", "IB_ENDPOINT"}
    columns = sorted(check_ids, key=lambda key: (list(_CHECK_LABELS).index(key) if key in _CHECK_LABELS else len(_CHECK_LABELS), key))
    states, hardware, software, reasons = [], [], [], []
    for record in records:
        node, complete = record["node"], _collection_complete(record)
        checks = {row["check_id"]: row for row in record.get("checks", [])}
        values = {key: (checks.get(key, {}).get("status", "NOT_APPLICABLE" if key in _CONDITIONAL_CHECKS else "UNKNOWN")
                        if complete else "UNKNOWN") for key in columns}
        values = {key: value if value in _STATUS_ORDER else "UNKNOWN" for key, value in values.items()}
        states.append(dict(node=node, values=values))
        if not complete:
            reasons.append(dict(node=node, values=dict(check="采集", status="UNKNOWN",
                detail=record.get("execution_detail") or "未取得本节点完整采集结果，详见原始执行日志")))
        else:
            for row in checks.values():
                if row["status"] not in {"PASS", "NOT_APPLICABLE"}:
                    reasons.append(dict(node=node, values=dict(check=_CHECK_LABELS.get(row["check_id"], row["check_id"]),
                        status=row["status"], detail=row["message"])))
        environment = record.get("environment") or {}
        hardware.append(dict(node=node, values=dict(devices=canonical_static_value(environment.get("hcu_devices", [])),
            collection=environment.get("hcu_hardware_status", "UNAVAILABLE"))))
        versions = {field: environment.get(field) for field, _ in _SOFTWARE_FIELDS}
        versions["torch_version"] = versions["torch_version"] or environment.get("python_packages", {}).get("torch")
        for field in ("transformer_engine_version", "deepep_version"):
            if complete and not versions[field]:
                versions[field] = "未检出版本"
        software.append(dict(node=node, values=versions))
    state_groups = _group_rows(states)
    for group in state_groups:
        group["status"] = _overall(group["values"].values())
    state_groups.sort(key=lambda group: (_STATUS_ORDER[group["status"]], _natural_key(group["nodes"][0])))
    return dict(node_count=len(records), collected_count=len(collected), checks=columns,
                status_counts=dict(Counter(_overall(row["values"].values()) for row in states)),
                state_groups=state_groups, reason_groups=_group_rows(reasons),
                hardware_groups=_group_rows(hardware), software_groups=_group_rows(software),
                consistency=consistency)


def _table(headers: list[str], rows: Iterable[list[str]]) -> list[str]:
    return ["| " + " | ".join(_cell(value) for value in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(row) + " |" for row in rows)]


def _status(value: str, markdown: bool) -> str:
    symbols = {"PASS": "🟢通过", "WARN": "🟡提示", "FAIL": "🔴失败", "UNKNOWN": "⚪未知", "NOT_APPLICABLE": "N/A不适用"}
    return symbols[value] if markdown else ("N/A" if value == "NOT_APPLICABLE" else value)


def _hardware_cells(values: dict) -> list[str]:
    devices = values["devices"]
    if not devices:
        return ["未采集"] * 4
    profiles = Counter(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in devices)
    models, units, memory = [], [], []
    for key, count in sorted(profiles.items()):
        device = json.loads(key)
        suffix = f"（{count}卡）" if len(profiles) > 1 else ""
        models.append(f"{device.get('model') or '未采集'} / {device.get('architecture') or '未采集'}{suffix}")
        units.append(f"{device['compute_units'] if device.get('compute_units') is not None else '未采集'}{suffix}")
        size = f"{device['memory_bytes'] / 1024**3:.2f} GiB" if device.get("memory_bytes") is not None else "未采集"
        memory.append(f"{size}{suffix}")
    count = str(len(devices)) + ("（采集不完整）" if values["collection"] != "COLLECTED" else "")
    return [count, "；".join(models), "；".join(units), "；".join(memory)]


def _overview_lines(report: dict, markdown: bool) -> list[str]:
    counts = report["status_counts"]
    lines = ["## ① 状态检查：节点集合为行，检测项为列" if markdown else "[PLATFORM] 状态与信息汇总", "",
             f"节点={report['node_count']}；采集完成={report['collected_count']}；未完成={report['node_count'] - report['collected_count']}。",
             "[状态] " + " ".join(f"{status}={counts.get(status, 0)}" for status in ("PASS", "WARN", "FAIL", "UNKNOWN")),
             "[信息] 硬件与软件独立归并；信息差异不作为状态失败依据。", ""]
    preferred = ["HYCU_DRIVER_MODULE", "HCU_DEVICE_NODE", "TORCH_HCU_AVAILABLE", "RDMA_DEVICE_COUNT", "IB_ENDPOINT", "IB_COUNTER_HEALTH"]
    columns = [key for key in preferred if key in report["checks"]] + [key for key in report["checks"] if key not in preferred]
    if not markdown:
        lines.extend(["[状态] 节点状态矩阵", ""])
    rows = [[_cell(_nodes(group["nodes"])), str(group["node_count"]),
             *(_status(group["values"][key], markdown) for key in columns), _status(group["status"], markdown)]
            for group in report["state_groups"]]
    lines.extend(_table(["节点集合", "节点数", *(_CHECK_LABELS.get(key, key) for key in columns), "总体状态"], rows))
    lines.extend(["", "### 异常与未知原因" if markdown else "[状态] 异常与未知原因", ""])
    if report["reason_groups"]:
        lines.extend(_table(["节点集合", "节点数", "检测项", "状态", "具体原因"],
            [[_cell(_nodes(group["nodes"])), str(group["node_count"]), _cell(group["values"]["check"]),
              _cell(group["values"]["status"]), _cell(group["values"]["detail"])] for group in report["reason_groups"]]))
    else:
        lines.append("无异常或未知原因。")
    lines.extend(["", "## ② 硬件信息：规格一致的节点合并展示" if markdown else "[信息] 硬件信息（rocminfo）", ""])
    lines.extend(_table(["类型", "配置组", "节点数", "节点集合", "卡数/节点", "型号／架构", "计算单元/卡", "显存/卡"],
        [["信息", f"H{index:02d}" if group["values"]["devices"] else "未取得规格", str(group["node_count"]), _cell(_nodes(group["nodes"])),
          *(_cell(value) for value in _hardware_cells(group["values"]))]
         for index, group in enumerate(report["hardware_groups"], 1)]))
    lines.extend(["", "型号使用 rocminfo 的原始值；多个内存池映射同一块显存时，不重复累加。"])
    reference = next((group["values"] for group in report["software_groups"]
                      if report["consistency"].get("reference_node") in group["nodes"]), {})
    lines.extend(["", "## ③ 软件信息：版本相同的节点合并，差异值加粗" if markdown else "[信息] 软件信息（版本差异由用户判断）", ""])
    rows = []
    for index, group in enumerate(report["software_groups"], 1):
        cells = []
        for field, _ in _SOFTWARE_FIELDS:
            value, baseline = group["values"].get(field), reference.get(field)
            different = value not in (None, "未检出版本") and baseline not in (None, "未检出版本") and value != baseline
            cells.append(f"**{_cell(value)}**" if different and markdown else ("≠ " if different else "") + _cell(value))
        label = f"S{index:02d}" + ("（参考）" if report["consistency"].get("reference_node") in group["nodes"] else "")
        if not any(group["values"].values()):
            label = "未采集"
        rows.append(["信息", label, str(group["node_count"]), _cell(_nodes(group["nodes"])), *cells])
    lines.extend(_table(["类型", "配置组", "节点数", "节点集合", *("驱动" if field == "driver_version" else label for field, label in _SOFTWARE_FIELDS)], rows))
    lines.extend(["", "未检出版本不等于未安装；包版本信息不代替导入或通信测试。",
                  "系统内存和共享内存容量保留原值，不参与一致性分组。"])
    if markdown:
        consistency = report["consistency"]
        lines.extend(["", "### 其他静态配置差异", "",
                      f"比较结论：{consistency.get('comparison_status', 'UNVERIFIED')}；参考节点：{_cell(consistency.get('reference_node'))}。"])
        for item in consistency.get("differences_from_reference", []):
            for field, values in item["changes"].get("platform", {}).items():
                if field in {name for name, _ in _SOFTWARE_FIELDS}:
                    continue
                lines.append(f"- 节点：{_cell(_nodes(item['nodes']))}；字段：{_cell(field)}；"
                             f"参考={_cell(values['reference'])}；实际={_cell(values['actual'])}")
        missing_groups = _group_rows(dict(node=node, values=missing)
            for node, missing in consistency.get("configuration_missing_by_node", {}).items())
        for group in missing_groups:
            lines.append(f"- {_cell(_nodes(group['nodes']))} 信息缺失：{_cell('、'.join(group['values']))}")
    return lines


def render_platform_markdown(report: dict) -> str:
    return "\n".join(_overview_lines(report, True)) + "\n"


def render_platform_terminal(report: dict) -> str:
    return "\n".join(_overview_lines(report, False))
