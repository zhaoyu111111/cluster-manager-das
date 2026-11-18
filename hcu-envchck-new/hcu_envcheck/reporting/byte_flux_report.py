"""解析逐节点byte-flux单项日志；保留执行与正确性、性能指标的边界。"""
from pathlib import Path
import json
import re
from ..concurrency import parallel_map
from .metrics import number
from .group_log import read_group, read_test
from .tables import atomic_text, read_table, table_text


def parse_node(job: tuple[Path, dict, int]) -> tuple[dict, list[dict]]:
    """按请求的卡数核对单节点 TP 启动、全部 rank 指标及结束证据。"""
    root, fact, gpus = job
    result, metrics = dict(fact), []
    if result['stage'] == 'prepare':
        return result, metrics
    sections, error = read_group(root, fact['target_id'])
    text, error = read_test(sections, error, fact)
    result['log'] = 'logs/groups/' + fact['target_id'] + '.log'
    if result['status'] == 'PASS' and error:
        result.update(status='FAIL', reason_code='GROUP_LOG_INVALID', detail=error)
    launches = re.findall(r'\[BYTE-FLUX-LAUNCH\] (\{[^\n]+\})', text)
    try:
        launch = json.loads(launches[0]) if len(launches) == 1 else {}
    except ValueError:
        launch = {}
    values = re.findall(r'flux\s+#(\d+): total ([0-9.]+) ms', text)
    torch_ranks = {int(rank) for rank in re.findall(r'torch\s+#(\d+): total', text)}
    ranks = {int(rank) for rank, _ in values}
    expected_ranks = set(range(gpus))
    if result['status'] == 'PASS':
        if not isinstance(launch, dict) or launch.get('workers') != gpus or launch.get('node') != result['node']:
            result.update(status='FAIL', reason_code='LAUNCH_EVIDENCE_INVALID', detail=f'缺少本节点 TP{gpus} 启动证据')
        elif ranks != expected_ranks or torch_ranks != expected_ranks:
            result.update(status='FAIL', reason_code='METRIC_COVERAGE_INCOMPLETE', detail=f'原生 {gpus} rank 输出或结束证据不完整')
        elif any(number(value, 0) <= 0 for _, value in values):
            result.update(status='FAIL', reason_code='METRIC_INVALID', detail='原生耗时必须为有限正数')
        else:
            result.update(reason_code='PASS', detail=f'TP{gpus} 原生正确性比较完成，单项执行成功')
    for rank, value in values:
        metrics.append(dict(node=result['node'], test=result['stage'], rank=rank, total_ms=value,
                            status=result['status'], reason_code=result['reason_code']))
    return result, metrics


def generate(root: Path) -> None:
    """复用公共并发池读取节点日志，由一个写入者发布派生结果和耗时表。"""
    facts = read_table(root / 'results.tsv')
    request = json.loads((root / 'plan/request.json').read_text(encoding='utf-8'))
    parsed = parallel_map(parse_node, [(root, fact, int(request['gpus_per_node'])) for fact in facts], int(request['concurrency']))
    results = [item[0] for item in parsed]
    metrics = [row for _, rows in parsed for row in rows]
    atomic_text(root / 'results.tsv', table_text(list(facts[0]), results))
    atomic_text(root / 'byte-flux-summary.tsv', table_text(
        ['node', 'test', 'rank', 'total_ms', 'status', 'reason_code'], metrics))
