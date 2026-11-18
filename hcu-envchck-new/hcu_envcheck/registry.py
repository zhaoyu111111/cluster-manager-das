"""显式操作注册表：只登记实际已迁移的功能，不做动态插件发现。"""
from __future__ import annotations
from importlib import import_module

OPERATIONS = ("container-status", "container-create", "container-recreate", "container-delete",
              "platform", "checkdeepep", "idle-check", "rccl", "deepep", "byte-flux")


def get_operation(name: str):
    """返回提供参数校验和阶段声明的操作模块。"""
    if name not in OPERATIONS:
        raise ValueError(f"尚未实现操作：{name}")
    module = "container" if name.startswith("container-") else name.replace("-", "_")
    return import_module(f"hcu_envcheck.operations.{module}")
