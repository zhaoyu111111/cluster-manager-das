"""容器参数校验、阶段声明及节点阶段结果解析；动作由宿主 container.sh 执行。"""
from __future__ import annotations
from pathlib import PurePosixPath
import re
from hcu_envcheck.models import TaskResult
from hcu_envcheck.operations._docker import CONTAINER_ID, IMAGE_ID, reject_options, validate_target


def validate(request) -> None:
    """校验操作范围、破坏性确认以及与固定生命周期参数冲突的 Docker 选项。"""
    validate_target(request)
    if request.operation not in {"container-status", "container-create", "container-recreate", "container-delete"}:
        raise ValueError("未实现的容器操作")
    if request.operation != "container-delete" and not request.image:
        raise ValueError("当前操作需要 -i/--image")
    reject_options(request, {"env_script", "container_workdir", "container_python",
                             "group_size", "gpus_per_node", "container_ssh_port", "skip_idle_check",
                             "tests", "baseline_file", "baseline_profile",
                             "deepep_source"})
    if request.operation in {"container-create", "container-recreate"}:
        if not 1 <= request.port <= 65535:
            raise ValueError("创建或重建需要有效 --port（1～65535）")
        if request.ssh_dir and not PurePosixPath(request.ssh_dir).is_absolute():
            raise ValueError("--ssh-dir 必须是目标宿主机可见的绝对路径")
    else:
        reject_options(request, {"port", "image_tar", "volumes", "docker_args", "ssh_dir", "container_user"})
    if request.operation in {"container-recreate", "container-delete"}:
        if not request.yes and not request.dry_run:
            raise ValueError("删除或重建指定容器需要显式 --yes")
    elif request.yes:
        raise ValueError("--yes 仅适用于删除或重建")
    if request.image_tar and not PurePosixPath(request.image_tar).is_absolute():
        raise ValueError("--image-tar 必须是目标宿主机可见的绝对路径")
    conflict = ("--network", "--net=", "--entrypoint", "--user", "--pid", "--mount",
                "--volume", "--name", "--publish", "--detach", "--rm", "--restart")
    flags = {"--init", "--read-only", "--oom-kill-disable", "--no-healthcheck"}
    for arg in request.docker_args:
        if arg.startswith(conflict) or arg == "--privileged=false":
            raise ValueError(f"--docker-arg 与公共容器参数冲突：{arg}")
        if arg not in flags and not re.fullmatch(r"--[A-Za-z][A-Za-z0-9-]*=.+", arg):
            raise ValueError("--docker-arg 请使用 --key=value；无值开关支持 --init、--read-only、--oom-kill-disable、--no-healthcheck")


def stages(request) -> list[str]:
    """声明原有有序阶段，由各节点 container.sh 顺序执行。"""
    return {
        "container-status": ["inspect"],
        "container-delete": ["preflight", "delete"],
        "container-create": ["preflight", "create", "ssh-identity"],
        "container-recreate": ["preflight", "replace", "create", "ssh-identity"],
    }[request.operation]


def interpret(node, target, stage: str, root, run_id: str) -> TaskResult:
    """读取节点阶段证据，保留每阶段退出码；缺失或身份错配不能通过。"""
    result = TaskResult(target.id, node.node, stage, "FAIL", "CONTAINER_STAGE_MISSING", 1,
                        detail="未取得本阶段完整结果", transport_exit_code=1)
    result.stdout, result.stderr = node.text(stage + ".log"), node.text(stage + ".stderr.log")
    status_path = node.results / (stage + ".status")
    if not status_path.is_file():
        if node.status != "PASS":
            result.reason_code, result.exit_code, result.transport_exit_code, result.detail = (
                node.reason, node.exit_code or 1, node.exit_code or 1, node.detail)
        return result
    try:
        cells = node.text(stage + ".status").rstrip("\n").split("\t")
        if len(cells) != 4 or cells[0] not in {"PASS", "WARN", "FAIL", "BLOCKED"} or not cells[2].isdigit():
            raise ValueError("阶段状态格式错误")
        status, reason, code, detail = cells
        meta = node.text(stage + ".meta").splitlines()
        token = f"{run_id}_{target.id}_{stage}"
        if len(meta) != 8 or meta[:4] != [run_id, target.id, stage, token]:
            raise ValueError("阶段运行、目标或 token 不匹配")
        container_id, image_id, started, ended = meta[4:]
        if container_id and not CONTAINER_ID.fullmatch(container_id):
            raise ValueError("容器完整 ID 无效")
        if image_id and not IMAGE_ID.fullmatch(image_id):
            raise ValueError("镜像完整 ID 无效")
        if not started.isdigit() or not ended.isdigit():
            raise ValueError("阶段时间无效")
        if status in {"PASS", "WARN"}:
            if int(code) or not image_id:
                raise ValueError("通过结果缺少完整镜像 ID 或包含非零退出码")
            if stage in {"inspect", "create", "delete", "ssh-identity"} and not container_id:
                raise ValueError("通过结果缺少容器完整 ID")
        if any(not (node.results / (stage + suffix)).is_file() for suffix in (".log", ".stderr.log")):
            raise ValueError("阶段日志未回传")
        result.status, result.reason_code, result.exit_code, result.detail = status, reason, int(code), detail
        result.container_id, result.image_id, result.token = container_id, image_id, token
        result.started_epoch, result.ended_epoch = int(started), int(ended)
        result.transport_exit_code = int(code)
    except (ValueError, OSError) as error:
        result.reason_code, result.detail = "CONTAINER_STAGE_INVALID", str(error)
    return result
