"""RCCL 操作声明：测试集合由上层选择，组内执行在 group_run.sh 和 rccl.sh。"""
from hcu_envcheck.models import Request
from hcu_envcheck.operations import _active

from hcu_envcheck.rccl_baseline import COLLECTIVES


def validate(request: Request) -> None:
    """校验 RCCL 单项集合；基准仅记录，留给 report 统一读取。"""
    _active.validate(request)
    if "deepep_source" in request.provided:
        raise ValueError("RCCL 不接受 DeepEP 参数：deepep_source")
    if not request.tests:
        request.tests = list(COLLECTIVES)
    if len(set(request.tests)) != len(request.tests) or any(item not in COLLECTIVES for item in request.tests):
        raise ValueError("RCCL tests 含未知项或重复项：" + ",".join(request.tests))


def stages(request: Request) -> list[str]:
    """返回准备阶段和用户指定的单项顺序。"""
    return ["prepare", *request.tests]
