"""容器名称、身份正则及公共参数校验；Docker 动作由节点端脚本执行。"""
from __future__ import annotations

import re


CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def validate_target(request) -> None:
    """校验只允许一个明确容器名和一个无歧义镜像名的公共约束。"""
    if request.scenario != "per-node-container":
        raise ValueError("当前操作仅支持 per-node-container")
    if not CONTAINER_NAME.fullmatch(request.container):
        raise ValueError("请通过 --container 提供合法容器名称")
    if request.image and (request.image.startswith("-") or any(c.isspace() for c in request.image)):
        raise ValueError("镜像名称不能以 - 开头或包含空白字符")


def reject_options(request, options: set[str]) -> None:
    """拒绝用户显式传入的无效选项，不将默认值误判为用户选择。"""
    invalid = set(getattr(request, "provided", ())) & options
    if invalid:
        raise ValueError("当前操作不接受 " + ", ".join("--" + key.replace("_", "-") for key in sorted(invalid)))
