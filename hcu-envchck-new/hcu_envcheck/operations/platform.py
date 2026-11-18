"""逐节点平台采集操作：执行在 node_run.sh，本模块只转换节点结论和采集元数据。

只执行原工程 platform 采集；健康规则和节点一致性由公共报告阶段处理。
不分组、不检查空闲、不安装软件，也不修改驱动、网络或容器配置。
"""
from __future__ import annotations

import json

from hcu_envcheck.models import NodeResult, TaskResult, is_container
from hcu_envcheck.reporting.tables import atomic_text


def validate(request) -> None:
    """接受三种场景，只保留采集、传输和目标环境参数，不接受性能/容器管理参数。"""
    if is_container(request) and not request.image:
        raise ValueError("platform 容器场景需要 -i/--image")
    invalid = request.provided & {"group_size", "gpus_per_node", "tests", "skip_idle_check", "baseline_file",
                                 "baseline_profile", "deepep_source", "dependency_manifest",
                                 "container_ssh_port", "yes", "port", "ssh_dir", "image_tar", "volumes", "docker_args"}
    if invalid:
        raise ValueError("platform 是逐节点只读检查，不接受参数：" + ", ".join(sorted(invalid)))
    if request.timeout < 30:
        raise ValueError("platform --timeout 至少 30 秒")


def stages(request) -> list[str]:
    """整份平台采集为每节点一个阶段。"""
    return ["platform"]


def interpret(node: NodeResult, target, stage: str, root, run_id: str) -> TaskResult:
    """保存采集元数据；健康结论由报告依据日志中的采集 JSON 判定。"""
    capability = node.text("container.capability").strip()
    metadata = dict(run_id=run_id, target_id=target.id, node=node.node, container_capabilities=capability,
                    network_host_scope_verified=capability == "host|true" or (not capability and node.status == "PASS"))
    atomic_text(root / ".state/platform" / f"{target.id}.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return TaskResult(target.id, node.node, stage, node.status, node.reason, node.exit_code, detail=node.detail,
                      stdout=node.text("node.log"), transport_exit_code=node.exit_code)
