# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：解析 RCCL 原始性能日志，校验测试覆盖和执行规模，并生成基准对比结果。
# 职责边界：将指标提取、正确性校验和性能判定分开，供最终报告逐组复用公共基准。
"""RCCL evidence parsing and verdicts; no execution, transport or fact writes.

调用方传入原始日志和计划规模；基准只由最终报告加载，不接收旧基准计划。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .metrics import number

METRIC_COLUMNS = ("out_of_place_algbw_gbps", "out_of_place_busbw_gbps",
                  "in_place_algbw_gbps", "in_place_busbw_gbps")


# 功能：保存单项 RCCL 的带宽、消息大小、rank 规模及输出完整性证据。
@dataclass
class RcclEvidence:
    metrics: dict
    planned_header: tuple | None
    observed: list[int]
    positive: list[int]
    expected: list[int]
    missing: list[int]


# 功能：单次遍历原始日志提取性能和规模证据，不加载基准或作性能判定。
def parse_log(source: Iterable[str], *, completed: bool = False) -> RcclEvidence:
    """Consume log lines once, without reading configuration or making a verdict."""
    # the full evidence set is parsed first — the planned size header,
    # every 13-column table row with both #wrong counters, the trailing
    # summary and the payload end marker. The summary line or a few
    # high-bandwidth rows can never stand in for the complete table, and
    # non-finite/malformed cells disqualify their row instead of feeding
    # the peaks. Performance is only judged after completeness and
    # correctness hold; the four columns stay independent peaks.
    peaks, buses, averages = [None] * 4, [], []
    observed, invalid_rows, wrong_out, wrong_in = [], 0, 0, 0
    planned_header = None
    ended = completed
    # actual execution scale evidence — every participating card prints
    # a "# Rank N Group G Pid P on <node> device D" line (single machine: one
    # process, nGpus cards; multi machine: one MPI rank per card). Planned
    # np never fills these fields; missing rank evidence blocks the verdict.
    rank_devices, rank_ids, rank_duplicates = [], [], 0
    header_gpus = None
    for line in source:
        header = re.search(r"minBytes (\d+) maxBytes (\d+) step: ([\d.]+)\(factor\)", line)
        if header and planned_header is None:
            planned_header = (int(header[1]), int(header[2]), float(header[3]))
        scale = re.search(r"nThread (\d+) nGpus (\d+)", line)
        if scale and header_gpus is None:
            header_gpus = int(scale[2])
        rank = re.match(r"#\s+Rank\s+(\d+)\s+Group\s+\d+\s+Pid\s+\d+\s+on\s+(\S+)\s+device\s+(\d+)", line)
        if rank:
            rank_ids.append(int(rank[1]))
            pair = (rank[2], int(rank[3]))
            if pair in rank_devices or int(rank[1]) in rank_ids[:-1]:
                rank_duplicates += 1
            else:
                rank_devices.append(pair)
        if "rccl test done" in line:
            ended = True
        match = re.search(r"Avg bus bandwidth\s*:\s*([\d.eE+-]+)", line)
        if match and number(match[1]) is not None:
            averages.append(number(match[1]))
        cells = line.split()
        if len(cells) >= 8 and cells[0].isdigit() and cells[1].isdigit():
            # Root/partitioned tests (gather/scatter/alltoall family) print
            # size-0 preamble rows and mark the in-place #wrong column N/A
            # (no in-place validation); N/A is the tool's contract, not an
            # error, while anything else non-numeric disqualifies the row.
            if len(cells) < 13 or not cells[8].isdigit() or not (cells[12].isdigit() or cells[12] == "N/A"):
                invalid_rows += 1
                continue
            values = [number(cells[i]) for i in (6, 7, 10, 11)]
            if any(value is None for value in values):
                invalid_rows += 1
                continue
            observed.append(int(cells[0]))
            peaks = [max(a, b) if a is not None else b for a, b in zip(peaks, values)]
            buses.extend((values[1], values[3]))
            wrong_out += int(cells[8])
            wrong_in += int(cells[12]) if cells[12].isdigit() else 0
    ladder = []
    if planned_header:
        minimum, maximum, factor = planned_header
        size = minimum
        while size <= maximum and len(ladder) < 128:
            ladder.append(size)
            size = int(size * factor) if factor > 1 else maximum + 1
    # The ladder is anchored at the header (minBytes..maxBytes); each test
    # type starts printing at its own effective first size (uniform tests at
    # minBytes, root tests at the first ladder member they actually run), so
    # completeness requires every ladder member from the observed start up.
    positive = sorted({size for size in observed if size > 0})
    expected = [size for size in ladder if positive and size >= positive[0]]
    missing = sorted(set(expected) - set(positive))
    nodes_seen = sorted({node for node, _ in rank_devices})
    per_node = [sum(1 for node, _ in rank_devices if node == member) for member in nodes_seen]
    actual_np = len(rank_devices)
    devices_per_node = (str(per_node[0]) if min(per_node) == max(per_node)
                        else f"{min(per_node)}-{max(per_node)}") if per_node else None

    metrics = dict(zip(METRIC_COLUMNS, peaks))
    metrics.update(peak_busbw_gbps=max(buses) if buses else None,
                   avg_busbw_gbps=averages[-1] if averages else None,
                   table_rows=len(observed), planned_sizes=len(expected),
                   missing_sizes=len(missing), invalid_rows=invalid_rows,
                   wrong_out=wrong_out, wrong_in=wrong_in, end_evidence=int(ended),
                   actual_np=actual_np, actual_nodes=len(nodes_seen),
                   devices_per_node=devices_per_node, rank_duplicates=rank_duplicates,
                   header_ngpus=header_gpus)
    return RcclEvidence(metrics, planned_header, observed, positive, expected, missing)


# 功能：检查包长覆盖、错误计数、实际执行规模和结束证据。
def validate_output(evidence: RcclEvidence, expected_np: float) -> tuple[str, str] | None:
    """Validate completeness, correctness and observed scale, in historical order."""
    m = evidence.metrics
    if not evidence.observed:
        return "DATA_TABLE_MISSING", "未解析到有效数据表（Avg摘要行不能替代完整表格）"
    if evidence.planned_header is None:
        return "PLAN_HEADER_MISSING", "日志缺少 minBytes/maxBytes/step 计划头，无法核对消息尺寸覆盖"
    if not evidence.positive:
        return "DATA_TABLE_INCOMPLETE", "数据表仅含0尺寸行，缺少实际消息尺寸证据"
    if m['invalid_rows']:
        return "DATA_ROWS_INVALID", f"{m['invalid_rows']} 行数据非有限值或列数/校验列损坏，不能作为有效证据"
    if evidence.missing:
        return "DATA_TABLE_INCOMPLETE", f"数据表缺 {len(evidence.missing)}/{len(evidence.expected)} 个计划尺寸（首缺 {evidence.missing[0]}B），疑似截断或漏跑"
    if m['wrong_out'] or m['wrong_in']:
        return "CORRECTNESS_FAILED", f"校验错误计数非零：out-of-place #wrong={m['wrong_out']}，in-place #wrong={m['wrong_in']}"
    if not m['end_evidence']:
        return "END_EVIDENCE_MISSING", "缺少完整测试结束证据，执行记录不完整"
    if not m['actual_np']:
        return "ACTUAL_SCALE_MISSING", "日志缺少 Rank/设备证据行，实际执行规模无法核验（不默认通过）"
    if m['rank_duplicates']:
        return "ACTUAL_SCALE_DUPLICATE", f"Rank/设备证据存在 {m['rank_duplicates']} 处重复"
    if m['actual_np'] != int(expected_np):
        return "SCALE_MISMATCH", f"计划 np={expected_np:g} 与实际 {m['actual_np']} 卡（{m['actual_nodes']} 节点×{m['devices_per_node']}卡）不符"
    return None


# 功能：将实测峰值与调用方给定的性能下限比较，返回缺失或未达标原因。
def evaluate_performance(peak: float | None, threshold: float) -> tuple[str, str] | None:
    """Compare validated bandwidth evidence with the saved threshold."""
    if peak is None:
        return "METRIC_MISSING", "未解析到带宽"
    if peak < threshold:
        return "BELOW_BASELINE", f"实测 {peak:.2f} GB/s，要求 ≥{threshold:.2f} GB/s"
    return None


# 功能：根据执行证据生成指标和判定结果，按调用选项执行性能对比，不改写原始事实。
def build_result(evidence: RcclEvidence, expected_np: int, fact: dict) -> dict:
    """核对一个已完成项的输出；性能基准仅在最终 compare_result() 处理。"""
    output = dict(fact)
    output['metrics'] = dict(evidence.metrics, np=expected_np)
    if output['status'] == 'PASS':
        issue = validate_output(evidence, expected_np)
        if issue is not None:
            output.update(status='FAIL', reason_code=issue[0], detail=issue[1])
        else:
            output.update(reason_code='PASS', detail='执行、规模及正确性检查通过，性能待判')
    return output


# 功能：在最终汇总时将单项指标与公共基准对比，并补充性能结论。
def compare_result(result: dict, reference: dict | None, error: str = '') -> None:
    """Judge measured data in place after all group logs have been parsed."""
    if result['status'] != 'PASS':
        return
    if reference is None:
        result.update(status='FAIL', reason_code='BASELINE_MISSING', detail=error or '无法加载公共基准')
        return
    peak = result['metrics'].get('peak_busbw_gbps')
    baseline, margin = float(reference['baseline_busbw_gbps']), float(reference['margin_pct'])
    threshold = baseline * (1 - margin / 100)
    issue = evaluate_performance(peak, threshold)
    result['metrics']['delta_pct'] = 100 * (peak - baseline) / baseline if peak is not None else None
    if issue:
        result.update(status='FAIL', reason_code=issue[0], detail=issue[1])
    else:
        result.update(reason_code='PASS', detail=f'{peak:.2f} GB/s ≥ {threshold:.2f} GB/s')
