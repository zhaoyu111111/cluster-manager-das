#!/usr/bin/env python3
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""在控制端并发读取 DeepEP 组日志，汇总结论和最差带宽，不执行测试。"""
from __future__ import annotations

from pathlib import Path
import re

from hcu_envcheck.reporting.metrics import number
from hcu_envcheck.reporting.tables import atomic_text, read_table, table_text
from hcu_envcheck.concurrency import parallel_map
from hcu_envcheck.reporting.group_log import read_group, read_test


def read_log(path: Path) -> str:
    """缺失日志返回空证据，由判断层保留未执行或不完整状态。"""
    return path.read_text(encoding='utf-8', errors='replace') if path.is_file() else ''


def failure_detail(text: str) -> tuple[str, str] | None:
    """识别实际失败的依赖、正确性或资源线索，不把低带宽推断为执行失败。"""
    unsupported = re.search(r'^\[DEEPEP-UNSUPPORTED\] (.+)$', text, re.MULTILINE)
    if unsupported:
        return 'DEEPEP_UNSUPPORTED_CONFIG', '未启动第三方测试：' + unsupported.group(1).strip()
    if 'TypeError' in text and ('incompatible function arguments' in text or 'get_nvl_buffer_size_hint' in text):
        return 'DEPENDENCY_API_MISMATCH', 'DeepEP 测试调用与已安装库的 API 不匹配，详见原始 traceback'
    if 'ModuleNotFoundError' in text or 'ImportError:' in text:
        return 'DEPENDENCY_MISSING', '测试依赖未安装或无法导入，详见原始 traceback'
    if 'hipIpcOpenMemHandle' in text and ('invalid device pointer' in text or 'Error' in text):
        return 'HIP_IPC_FAILED', '目标 RocSHMEM/HIP 运行时报告 IPC 内存句柄错误，详见原始日志'
    if 'out of memory' in text.lower():
        return 'OUT_OF_MEMORY', '测试进程报告显存不足'
    if 'AssertionError' in text:
        return 'CORRECTNESS_FAILED', '第三方测试断言失败，详见原始 traceback'
    return None


def parse_group(job: tuple[Path, dict, list[dict]]) -> tuple[list[dict], list[dict], list[dict]]:
    """解析一个组的环境检查和每个模式，保留原始失败；组之间并发。"""
    root, group, facts = job
    results, metrics, environment = [], [], []
    sections, group_error = read_group(root, group["group_id"])
    prep = read_log(root / 'logs/prepare' / (group['group_id'] + '.stdout.log'))
    for node, code in re.findall(r'__HCU_DEEPEP_ENV__\t([^\t\n]+)\t([0-9]+)', prep):
        environment.append(dict(group_id=group['group_id'], node=node, exit_code=code,
                                status='PASS' if code == '0' else 'WARN' if code == '2' else 'FAIL'))
    for fact in facts:
        result = dict(fact)
        mode = fact['stage']
        if mode == 'prepare':
            if result['status'] == 'PASS':
                expected = set(group['nodes'].split(','))
                if len(environment) != len(expected) or {row['node'] for row in environment} != expected:
                    result.update(status='FAIL', reason_code='CHECK_OUTPUT_INCOMPLETE', detail='缺少组员的原子环境检查结论')
                elif any(row['status'] == 'FAIL' for row in environment):
                    result.update(status='FAIL', reason_code='CHECK_FAILED', detail='组员环境检查包含 FAIL')
                elif any(row['status'] == 'WARN' for row in environment):
                    result.update(status='WARN', reason_code='CHECK_WARN', detail='原子环境检查包含 WARN，原始检测结论已保留')
            results.append(result)
            continue
        stdout, log_error = read_test(sections, group_error, fact)
        result['log'] = 'logs/groups/' + group['group_id'] + '.log'
        if result["status"] == "PASS" and log_error:
            result.update(status="FAIL", reason_code="GROUP_LOG_INVALID", detail=log_error)
        stderr = read_log(root / 'logs' / mode / (group['group_id'] + '.stderr.log'))
        pattern = (r'\[rank (\d+)\] Dispatch \+ combine bandwidth:\s*([0-9]+(?:\.[0-9]+)?) GB/s' if mode == 'lowl'
                   else r'\[tuning\] Best dispatch.*?([0-9]+(?:\.[0-9]+)?) GB/s \((?:RDMA|NVL)\)')
        matches = re.findall(pattern, stdout)
        values = [number(match[1] if mode == 'lowl' else match) for match in matches]
        valid = [value for value in values if value is not None]
        bandwidth = min(valid) if valid else None
        launches = re.findall(r'\[DEEPEP-LAUNCH\] node=(\S+) nnodes=(\d+) node_rank=(\d+) gpu_workers=(\d+) mode=(\S+)', stdout)
        if result['status'] == 'PASS':
            issue = failure_detail(stdout + '\n' + stderr)
            if issue:
                result.update(status='FAIL', reason_code=issue[0], detail=issue[1])
            elif (len(launches) != int(group['node_count']) or {row[0] for row in launches} != set(group['nodes'].split(','))
                  or {int(row[2]) for row in launches} != set(range(int(group['node_count'])))
                  or any(int(row[1]) != int(group['node_count']) or row[3] != group['gpus_per_node'] or row[4] != mode for row in launches)):
                result.update(status='FAIL', reason_code='LAUNCH_EVIDENCE_INVALID', detail='各节点启动参数与分组规模不一致或不完整')
            elif not values or len(valid) != len(values) or bandwidth is None or bandwidth <= 0:
                result.update(status='FAIL', reason_code='METRIC_MISSING', detail='未取得有效的固定模式带宽指标')
            elif mode == 'lowl' and {int(match[0]) for match in matches} != set(range(int(group['np']))):
                result.update(status='FAIL', reason_code='METRIC_COVERAGE_INCOMPLETE', detail='低延迟模式缺少部分 GPU rank 的带宽样本')
            else:
                result.update(reason_code='PASS', detail=f'测试完成；最差带宽 {bandwidth:.2f} GB/s，{len(values)} 个样本')
        elif result['reason_code'] == 'DEEPEP_EXECUTION_FAILED':
            issue = failure_detail(stdout + '\n' + stderr)
            if issue:
                result.update(reason_code=issue[0], detail=issue[1])
        results.append(result)
        metrics.append(dict(group_id=group['group_id'], nodes=group['nodes'], mode=mode,
                            planned_gpu_workers=group['np'], gpus_per_node=group['gpus_per_node'],
                            observed_control_nodes=len(launches), bandwidth_gbps=bandwidth, metric_rule='min',
                            metric_samples=len(values), metric_unit='GB/s', status=result['status'],
                            reason_code=result['reason_code'], exit_code=fact['exit_code'], detail=result['detail']))
    return results, metrics, environment


def generate(root: Path) -> int:
    """仅在公共执行已关闭后，由一个写入者合并所有组的解析结果和环境明细。"""
    request = {row['key']: row['value'] for row in read_table(root / 'plan/request.tsv')}
    groups, facts = read_table(root / 'plan/groups.tsv'), read_table(root / 'results.tsv')
    if not groups or not facts:
        raise ValueError('DeepEP 分组或执行结果为空')
    jobs = [(root, group, [fact for fact in facts if fact['target_id'] == group['group_id']]) for group in groups]
    outputs = parallel_map(parse_group, jobs, int(request['concurrency']))
    results = {(row['target_id'], row['stage']): row for output in outputs for row in output[0]}
    metrics = [row for output in outputs for row in output[1]]
    environments = [row for output in outputs for row in output[2]]
    atomic_text(root / 'results.tsv', table_text(list(facts[0]), [results[(row['target_id'], row['stage'])] for row in facts]))
    fields = ['group_id', 'nodes', 'mode', 'planned_gpu_workers', 'gpus_per_node', 'observed_control_nodes',
              'bandwidth_gbps', 'metric_rule', 'metric_samples', 'metric_unit', 'status', 'reason_code', 'exit_code', 'detail']
    atomic_text(root / 'deepep-summary.tsv', table_text(fields, metrics))
    atomic_text(root / 'deepep-environment.tsv', table_text(['group_id', 'node', 'exit_code', 'status'], environments))
    for detail in dict.fromkeys(row['detail'] for row in metrics
                                if row['reason_code'] == 'DEEPEP_UNSUPPORTED_CONFIG'):
        print(f'[DEEPEP-UNSUPPORTED] {detail}', flush=True)
    print(f'[DEEPEP-REPORT] groups={len(groups)} cases={len(metrics)}', flush=True)
    return 0


