"""整体执行原四项 DeepEP 环境检查；执行在 node_run.sh，本模块只转换节点结论。

只读检测不要求目标 Python，不安装依赖；原脚本的 0/1/2 和四项内容保持不变。
"""
from __future__ import annotations

from hcu_envcheck.models import NodeResult, TaskResult
from hcu_envcheck.operations._docker import reject_options, validate_target


def validate(request) -> None:
    """限制为逐节点只读原子检查；目标 Shell 无需选择 Python。"""
    validate_target(request)
    if not request.image:
        raise ValueError("checkdeepep 需要 -i/--image")
    reject_options(request, {"yes", "port", "image_tar", "volumes", "docker_args", "ssh_dir",
                             "group_size", "gpus_per_node", "container_ssh_port", "skip_idle_check",
                             "tests", "container_python"})


def stages(request) -> list[str]:
    """ACS、XDP、RDMA 和 verbs 共四项，整体只声明一个执行阶段。"""
    return ["checkdeepep"]


def interpret(node: NodeResult, target, stage: str, root, run_id: str) -> TaskResult:
    """发布本节点原始输出和真实命令码，最终四项汇总由报告解释。"""
    return TaskResult(target.id, node.node, stage, node.status, node.reason, node.exit_code,
                      detail=node.detail, stdout=node.text("node.log"), transport_exit_code=node.exit_code)
