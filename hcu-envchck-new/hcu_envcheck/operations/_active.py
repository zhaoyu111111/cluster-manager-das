"""RCCL/DeepEP/byte-flux 共同的参数校验；执行由 dispatch 和目标端 payload 完成。"""
from __future__ import annotations

import re

from hcu_envcheck.models import Request, is_container, peer_port


def validate(request: Request) -> None:
    """校验主动测试共同的目标路径与规模，容器身份参数仅在容器场景要求。"""
    if is_container(request) and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", request.container) or not request.image):
        raise ValueError("主动测试需要 --container 和 --image")
    invalid = request.provided & {"yes", "port", "image_tar", "volumes", "docker_args", "ssh_dir"}
    if invalid:
        raise ValueError("主动测试不接受容器管理参数：" + ", ".join(sorted(invalid)))
    if request.group_size < 1 or not 1 <= request.gpus_per_node <= 1024:
        raise ValueError("group-size 必须为正整数，gpus-per-node 必须为 1～1024")
    if request.timeout < 60 or not 1 <= peer_port(request) <= 65535:
        raise ValueError("主动测试 timeout 至少 60 秒，节点间 SSH 端口须为 1～65535")
    for name in ("env_script", "container_workdir", "remote_workdir"):
        value = getattr(request, name)
        if value and (not value.startswith("/") or any(c in value for c in "\n\r\t")):
            raise ValueError(f"{name} 必须为目标环境内的绝对路径")
    if any(part in (".", "..") for part in request.remote_workdir.split("/")):
        raise ValueError("remote-workdir 不能含 . 或 .. 路径段")
    if request.remote_workdir in ("/", "/tmp", "/var", "/home", "/root"):
        raise ValueError("remote-workdir 必须是独立工具目录，例如 /tmp/hcu-envcheck")
