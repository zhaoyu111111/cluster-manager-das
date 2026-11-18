"""读取节点并生成有序执行计划；不访问远端，也不执行检测。"""
from __future__ import annotations
from dataclasses import asdict
import json
from pathlib import Path
import re

from .models import ACTIVE_OPERATIONS, Plan, Request, Target
from .reporting.tables import atomic_text, table_text

PREVIEW_TARGETS = 5


def read_hosts(path: str) -> list[str]:
    """保留节点顺序，支持空行和注释，拒绝重复节点及额外字段。"""
    nodes, seen = [], set()
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[0]):
            raise ValueError(f"节点文件每行只允许一个合法节点名：{line}")
        if parts[0] in seen:
            raise ValueError(f"重复节点：{parts[0]}")
        seen.add(parts[0])
        nodes.append(parts[0])
    if not nodes:
        raise ValueError("节点文件为空")
    return nodes


def build_plan(request: Request, operation) -> Plan:
    """仅 RCCL/DeepEP 调用分组；其他操作直接消费 hostfile 中节点。"""
    nodes = read_hosts(request.hostfile)
    grouped = request.operation in ("rccl", "deepep")
    size = request.group_size if grouped else 1
    complete_count = len(nodes) // size * size
    if not complete_count:
        raise ValueError(f"没有可执行的完整分组：共 {len(nodes)} 个节点，不足 group-size={size}；"
                         f"跳过节点：{','.join(nodes)}")
    targets = [Target(f"{'group' if grouped else 'node'}-{i // size + 1:03d}", tuple(nodes[i:i + size]))
               for i in range(0, complete_count, size)]
    plan = Plan(targets, operation.stages(request), grouped, skipped_nodes=nodes[complete_count:])
    if hasattr(operation, "validate_plan"):
        operation.validate_plan(request, plan)
    return plan


def request_dict(request: Request) -> dict:
    """生成可持久化请求，不携带进程环境或凭据内容。"""
    data = asdict(request)
    data["provided"] = sorted(request.provided)
    return data


def write_plan(root: Path, request: Request, plan: Plan) -> None:
    """保存 JSON 和便于人工查看的 TSV/节点文件，执行与报告使用同一计划。"""
    directory = root / "plan"
    directory.mkdir(parents=True)
    data = request_dict(request)
    atomic_text(directory / "request.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    values = {key: ",".join(value) if isinstance(value, list) else str(int(value)) if isinstance(value, bool)
              else str(value) for key, value in data.items()}
    atomic_text(directory / "request.tsv", table_text(["key", "value"],
                                                     [{"key": k, "value": v} for k, v in values.items()]))
    atomic_text(directory / "targets.tsv", table_text(["target_id", "node"],
                [{"target_id": t.id, "node": t.leader} for t in plan.targets]))
    atomic_text(directory / "stages.tsv", table_text(["stage_order", "stage"],
                [{"stage_order": i, "stage": stage} for i, stage in enumerate(plan.stages, 1)]))
    atomic_text(directory / "all.hosts", "".join(node + "\n" for target in plan.targets for node in target.nodes))
    if plan.skipped_nodes:
        atomic_text(directory / "skipped.hosts", "".join(node + "\n" for node in plan.skipped_nodes))
    for start in range(0, len(plan.targets), request.concurrency):
        batch = plan.targets[start:start + request.concurrency]
        atomic_text(directory / f"clushhostfile.{start // request.concurrency + 1:04d}",
                    "".join(target.leader + "\n" for target in batch))
    if request.operation in ACTIVE_OPERATIONS:
        rows = []
        for target in plan.targets:
            group = directory / "groups" / target.id
            group.mkdir(parents=True)
            atomic_text(group / "hostfile", "".join(node + "\n" for node in target.nodes))
            slots = 1 if request.operation == "deepep" else request.gpus_per_node
            atomic_text(group / "slotsfile", "".join(f"{node} slots={slots}\n" for node in target.nodes))
            rows.append(dict(group_id=target.id, leader=target.leader, node_count=len(target.nodes),
                             gpus_per_node=request.gpus_per_node, np=len(target.nodes) * request.gpus_per_node,
                             nodes=",".join(target.nodes)))
        atomic_text(directory / "groups.tsv", table_text(
            ["group_id", "leader", "node_count", "gpus_per_node", "np", "nodes"], rows))


def preview(request: Request, plan: Plan) -> None:
    """展示实际目标、阶段和规模；dry-run 不建立目录也不连接服务器。"""
    environment = request.scenario + (f" / {request.container}" if request.container else "")
    print(f"计划：{request.operation}；目标环境：{environment}；clush 并发：{request.concurrency}")
    for target in plan.targets[:PREVIEW_TARGETS]:
        scale = f"；{len(target.nodes)}节点 × {request.gpus_per_node}卡" if plan.grouped else ""
        print(f"  {target.id}: {','.join(target.nodes)}{scale}")
    if len(plan.targets) > PREVIEW_TARGETS:
        print(f"  …… 共 {len(plan.targets)} 个目标")
    if plan.skipped_nodes:
        print(f"跳过节点：{','.join(plan.skipped_nodes)}；原因：尾组仅 {len(plan.skipped_nodes)} 个节点，"
              f"不足 group-size={request.group_size}")
    print("阶段：" + " → ".join(plan.stages))
    print(f"执行批次：{(len(plan.targets) + request.concurrency - 1) // request.concurrency}；每批最多 {request.concurrency} 个组首节点，逐批执行")
    if request.operation == "idle-check":
        print("检测范围：宿主机全部卡；释放占卡容器：" + ("是（停止整个容器后复检）" if request.release else "否（只读）"))
