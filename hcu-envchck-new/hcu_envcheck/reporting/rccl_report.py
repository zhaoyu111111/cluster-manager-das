#!/usr/bin/env python3
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""在控制端并发解析各组日志，最后加载一次公共基准并发布派生结果。

原始执行事实保持只读；Python 只写 reporting 的结果文件，不启动任何测试。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from hcu_envcheck.reporting.tables import atomic_text, read_table, table_text
from hcu_envcheck.rccl_baseline import BaselineConfigError, load_baseline
from hcu_envcheck.reporting.rccl_output import METRIC_COLUMNS, build_result, compare_result, parse_log
from hcu_envcheck.concurrency import parallel_map
from hcu_envcheck.reporting.group_log import read_group, read_test


def parse_group(arguments: tuple[Path, dict, list[dict]]) -> list[tuple[dict, dict]]:
    """串行读一个组的各项日志；组与组之间由线程池并发解析。"""
    root, group, executions = arguments
    output = []
    sections, group_error = read_group(root, group["group_id"])
    for fact in executions:
        if fact['stage'] == 'prepare':
            output.append((dict(fact), {}))
            continue
        text, log_error = read_test(sections, group_error, fact)
        fact = dict(fact, log='logs/groups/' + group['group_id'] + '.log')
        evidence = parse_log(text.splitlines(), completed=not log_error and int(fact["transport_exit_code"]) == 0)
        if fact['status'] == 'PASS' and log_error:
            output.append((dict(fact, status='FAIL', reason_code='GROUP_LOG_INVALID', detail=log_error), dict(evidence.metrics)))
            continue
        if fact['status'] == 'PASS':
            result = build_result(evidence, int(group['np']), fact)
            metrics = result.pop('metrics')
        else:
            result, metrics = dict(fact), dict(evidence.metrics)
        if result['status'] == 'PASS' and (metrics['actual_nodes'] != int(group['node_count'])
                                          or metrics['devices_per_node'] != group['gpus_per_node']):
            result.update(status='FAIL', reason_code='SCALE_MISMATCH', detail='实际节点数或每节点卡数与分组计划不一致')
        output.append((result, metrics))
    return output


def resolve_reference(request: dict, root: Path) -> tuple[str | None, str, dict]:
    """报告阶段只读取一份基准原文；未配置与显式配置错误分开处理。"""
    explicit = request.get('baseline_file') or os.environ.get('RCCL_BASELINE_FILE', '')
    path = Path(explicit or (Path(request['root']) / 'config/rccl_baseline.conf'))
    profile = request.get('baseline_profile') or os.environ.get('RCCL_BASELINE_PROFILE', '')
    metadata = {'source': str(path), 'profile': profile, 'error': ''}
    if not path.is_file() and not explicit:
        return None, profile, metadata
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as error:
        metadata['error'] = str(error)
        return None, profile, metadata
    metadata['sha256'] = hashlib.sha256(text.encode()).hexdigest()
    atomic_text(root / 'baseline-used.conf', text)
    return text, profile, metadata


def generate(root: Path) -> int:
    """全部组解析完毕后统一对比缓存基准，由一个写入者发布最终表和指标。"""
    request = {row['key']: row['value'] for row in read_table(root / 'plan/request.tsv')}
    groups = read_table(root / 'plan/groups.tsv')
    facts = read_table(root / 'results.tsv')
    if not facts or not groups:
        raise ValueError('RCCL 分组或执行结果为空')
    fields = list(facts[0])
    jobs = [(root, group, [fact for fact in facts if fact['target_id'] == group['group_id']]) for group in groups]
    parsed = dict(zip((group['group_id'] for group in groups),
                      parallel_map(parse_group, jobs, int(request['concurrency']))))
    text, profile, provenance = resolve_reference(request, root)
    reference_cache = {}
    results, metrics_rows = {}, []
    for group in groups:
        shape = (int(group['node_count']), int(group['gpus_per_node']))
        if shape not in reference_cache:
            error, references = provenance['error'], {}
            if text is not None:
                try:
                    references = {row.test: {'baseline_busbw_gbps': float(row.baseline), 'margin_pct': float(row.error_pct)}
                                  for row in load_baseline(text, profile=profile, nodes=shape[0], gpus_per_node=shape[1])}
                except BaselineConfigError as exception:
                    error = str(exception)
            reference_cache[shape] = references, error
        references, error = reference_cache[shape]
        for result, metrics in parsed[group['group_id']]:
            item = result['stage']
            if item != 'prepare':
                reference = references.get(item)
                if result['status'] == 'PASS':
                    if text is None and not error:
                        result.update(status='WARN', reason_code='BASELINE_NOT_CONFIGURED', detail='未配置基准，仅汇报实测性能')
                    else:
                        # 调用原工程的相同阈值规则：只对性能下降超过 error_pct 的项目报错。
                        result['metrics'] = metrics
                        compare_result(result, reference, error)
                        result.pop('metrics')
                metrics_rows.append(dict(group_id=group['group_id'], nodes=group['nodes'], test=item,
                                         np=group['np'], **{key: value for key, value in metrics.items() if key != 'np'},
                                         baseline_busbw_gbps=reference['baseline_busbw_gbps'] if reference else None,
                                         error_pct=reference['margin_pct'] if reference else None,
                                         status=result['status'], reason_code=result['reason_code']))
            results[(result['target_id'], item)] = result
    atomic_text(root / 'results.tsv', table_text(fields, [results[(row['target_id'], row['stage'])] for row in facts]))
    metric_fields = ['group_id', 'nodes', 'test', 'np', *METRIC_COLUMNS, 'peak_busbw_gbps', 'avg_busbw_gbps',
                     'actual_np', 'actual_nodes', 'devices_per_node', 'table_rows', 'planned_sizes', 'missing_sizes',
                     'wrong_out', 'wrong_in', 'baseline_busbw_gbps', 'error_pct', 'delta_pct', 'status', 'reason_code']
    atomic_text(root / 'rccl-summary.tsv', table_text(metric_fields, metrics_rows))
    provenance['layouts'] = [{'node_count': shape[0], 'gpus_per_node': shape[1], 'error': value[1]}
                             for shape, value in reference_cache.items()]
    atomic_text(root / 'baseline-source.json', json.dumps(provenance, ensure_ascii=False, indent=2) + '\n')
    print(f'[RCCL-REPORT] groups={len(groups)} tests={len(metrics_rows)} profile={profile or "未指定"}', flush=True)
    return 0


