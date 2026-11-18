"""唯一参数解析入口：校验请求后交给计划和控制器，不包含远端操作。"""
from __future__ import annotations
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import sys

from .models import Request
from .planning import build_plan, preview
from .registry import OPERATIONS, get_operation


def make_parser() -> argparse.ArgumentParser:
    """定义已实现功能和参数，帮助信息不依赖 Docker 或目标环境。"""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog="hcu-cluster-run", description="集群 Conda/容器环境与性能检测及容器管理")
    parser.add_argument("--version", action="version", version=(root / "VERSION").read_text().strip())
    parser.add_argument("--help-full", action="help", help="显示全部参数")
    parser.add_argument("scenario", choices=["shared-conda", "node-local-conda", "per-node-container"])
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("-f", "--hostfile", required=True, help="启动端节点文件，每行一个节点")
    parser.add_argument("--container", default="", help="容器场景必填：已有或待管理的容器名称")
    parser.add_argument("-i", "--image", default="", help="预期镜像名称")
    parser.add_argument("-o", "--output-dir", default="", help="控制端报告父目录")
    parser.add_argument("--concurrency", type=int, default=1024, help="每批最多执行组数及clush并发连接数(-f)，逐批执行，默认1024")
    parser.add_argument("--timeout", type=int, default=1800, help="单个测试项超时秒数")
    parser.add_argument("--connect-timeout", type=int, default=15)
    parser.add_argument("--ssh-user", default=os.environ.get("HCU_SSH_USER", ""))
    parser.add_argument("--ssh-port", type=int, default=22, help="宿主SSH端口；Conda多节点MPI也使用此端口，默认22")
    parser.add_argument("--ssh-config", default="")
    parser.add_argument("--ssh-strict", choices=["yes", "accept-new"], default="yes")
    parser.add_argument("--group-size", type=int, default=1, help="RCCL/DeepEP每组节点数")
    parser.add_argument("--gpus-per-node", type=int, default=8)
    parser.add_argument("--tests", default="", help="逗号分隔的单项测试名")
    parser.add_argument("--container-ssh-port", type=int, default=25901, help="容器间MPI SSH端口，默认25901")
    parser.add_argument("--env-script", default="", help="目标环境内脚本绝对路径，进入后source")
    parser.add_argument("--target-workdir", "--container-workdir", dest="container_workdir", default="", help="目标环境工作目录，默认本次任务目录")
    parser.add_argument("--target-python", "--container-python", dest="container_python", default="python3", help="source后使用的目标Python命令或绝对路径")
    parser.add_argument("--remote-workdir", default="/tmp/hcu-envcheck", help="目标环境内本次任务目录的父目录")
    parser.add_argument("--baseline-file", default="", help="控制端RCCL基准文件，由最终report读取")
    parser.add_argument("--baseline-profile", default="", help="明确选择RCCL基准布局")
    parser.add_argument("--deepep-source", default="", help="控制端DeepEP源码，默认本工程third_party/DeepEP")
    parser.add_argument("--release", action="store_true", help="仅idle-check：停止已核实的占卡容器并重新检查宿主空闲")
    parser.add_argument("--skip-idle-check", action="store_true", help="明确跳过宿主机空闲门禁")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不连接节点")
    parser.add_argument("--yes", action="store_true", help="确认删除/重建指定容器")
    parser.add_argument("--port", type=int, default=25901, help="容器创建时的sshd端口")
    parser.add_argument("--ssh-dir", default="", help="目标宿主SSH材料，默认各节点登录用户的HOME/.ssh；复制而非挂载")
    parser.add_argument("--container-user", default="", help="容器执行用户，默认镜像用户；与宿主SSH用户独立")
    parser.add_argument("--dependency-manifest", default="", help="目标环境构建来源清单；也可由env.sh设置HCU_DEPENDENCY_MANIFEST")
    parser.add_argument("--flux-source", default="", help="控制端flux源码，默认本工程third_party/flux")
    parser.add_argument("--image-tar", default="", help="宿主机可见的离线镜像绝对路径")
    parser.add_argument("-v", "--volume", action="append", dest="volumes", default=[])
    parser.add_argument("--docker-arg", action="append", dest="docker_args", default=[])
    return parser


def validate_common(request: Request) -> None:
    """检查本地输入与目标路径语义，不把宿主路径误当作容器路径。"""
    for name in ("concurrency", "timeout", "connect_timeout", "group_size", "gpus_per_node"):
        if getattr(request, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 必须为正整数")
    if request.concurrency > 8192 or request.gpus_per_node > 1024:
        raise ValueError("并发不能超过8192，每节点卡数不能超过1024")
    for name in ("ssh_port", "container_ssh_port", "port"):
        if not 1 <= getattr(request, name) <= 65535:
            raise ValueError(f"--{name.replace('_', '-')} 应为1～65535")
    if request.ssh_user and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", request.ssh_user):
        raise ValueError("SSH用户名格式错误")
    if request.release and request.operation != "idle-check":
        raise ValueError("--release 仅适用于 idle-check")
    if request.operation == "idle-check":
        # idle-check 实际检测宿主机：三种场景入口均可，不要求容器身份或环境脚本。
        pass
    elif request.scenario == "per-node-container":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", request.container):
            raise ValueError("容器场景需要合法的 --container")
    else:
        if request.operation not in ("platform", "rccl", "deepep", "byte-flux"):
            raise ValueError(f"{request.operation} 仅支持 per-node-container")
        invalid = request.provided & {"container", "image", "container_user", "container_ssh_port",
                                      "yes", "port", "ssh_dir", "image_tar", "volumes", "docker_args"}
        if invalid:
            raise ValueError("Conda场景不接受容器参数：" + ", ".join(sorted(invalid)))
        if not request.env_script:
            raise ValueError("Conda场景需要 --env-script 显式加载目标环境")
    for name in ("env_script", "container_workdir", "remote_workdir", "ssh_dir", "image_tar"):
        value = getattr(request, name)
        if value and (not value.startswith("/") or ".." in PurePosixPath(value).parts):
            raise ValueError(f"--{name.replace('_', '-')} 必须为目标环境绝对路径且不能含..")
    if request.remote_workdir in ("/", "/tmp", "/var", "/home", "/public") or any(c.isspace() for c in request.remote_workdir):
        raise ValueError("remote-workdir必须是专供工具使用的子目录，且不能含空白")
    if request.container_python.startswith("-") or any(c.isspace() for c in request.container_python):
        raise ValueError("target-python只能为一个命令名或绝对路径")
    for name in ("hostfile", "ssh_config", "baseline_file"):
        value = getattr(request, name)
        if value:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"控制端文件不可读取：{path}")
            setattr(request, name, str(path))
    if request.container_user and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*|[0-9]+(?::[0-9]+)?",request.container_user):
        raise ValueError("container-user必须是用户名或UID[:GID]")
    owners = {"deepep_source": {"deepep"}, "flux_source": {"byte-flux"}, "dependency_manifest": {"deepep", "byte-flux"},
              "baseline_file": {"rccl"}, "baseline_profile": {"rccl"}}
    for option, operations in owners.items():
        if option in request.provided and request.operation not in operations:
            raise ValueError(f"--{option.replace('_', '-')} 仅适用于 {', '.join(sorted(operations))}")
    for field, library in (("deepep_source","DeepEP"),("flux_source","flux")):
        value=getattr(request,field)
        setattr(request,field,str(Path(value).expanduser().resolve()) if value else str(Path(request.root)/"third_party"/library))
    if request.dependency_manifest and not request.dependency_manifest.startswith("/"):
        raise ValueError("dependency-manifest必须为目标环境绝对路径")
    for name, value in vars(request).items():
        if isinstance(value, str) and any(char in value for char in "\r\n\t\x00"):
            raise ValueError(f"{name}不能包含控制字符")


def main(argv: list[str] | None = None) -> int:
    """组装明确的四步流程；参数错误和运行报告错误均给出可读原因。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = make_parser()
    if not arguments:
        parser.print_help()
        return 0
    namespace = parser.parse_args(arguments)
    # 仅用 argparse 的正常解析区分显式选项与默认值，包含 -v/path 和 --key=value。
    explicit_parser = make_parser()
    for action in explicit_parser._actions:
        if action.option_strings:
            action.default = argparse.SUPPRESS
    provided = set(vars(explicit_parser.parse_args(arguments))) - {"scenario", "operation"}
    data = vars(namespace)
    tests = data.pop("tests")
    data["tests"] = tests.split(",") if tests else []
    request = Request(root=str(Path(__file__).resolve().parents[1]), provided=provided, **data)
    try:
        validate_common(request)
        operation = get_operation(request.operation)
        operation.validate(request)
        plan = build_plan(request, operation)
        preview(request, plan)
        if request.dry_run:
            return 0
        from .controller import run
        return run(request, plan, operation)
    except (ValueError, OSError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2
