"""byte-flux 逐节点运行一个 FP16 AG 用例，TP 规模取 gpus_per_node。"""
from hcu_envcheck.operations import _active


def validate(request) -> None:
    """不开放用例和形状；每节点使用参数指定数量的卡执行一次。"""
    _active.validate(request)
    if request.group_size != 1:
        raise ValueError("byte-flux 逐节点执行，--group-size 必须为 1")
    if request.tests:
        raise ValueError("byte-flux 不接受 --tests")
    request.tests = [f"tp{request.gpus_per_node}"]
    if request.provided & {"baseline_file", "baseline_profile", "deepep_source"}:
        raise ValueError("byte-flux不接受RCCL/DeepEP专有参数")


def stages(request) -> list[str]:
    """准备后执行参数所指定 TP 规模的单项测试。"""
    return ["prepare", *request.tests]
