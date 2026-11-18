"""DeepEP 操作声明：校验单机/多机布局，组内执行在 group_run.sh 和 deepep.sh。"""
from hcu_envcheck.models import Request
from hcu_envcheck.operations import _active


def validate(request: Request) -> None:
    """保留请求项并逐项校验支持范围；全部不支持时在控制端报错。"""
    _active.validate(request)
    invalid = request.provided & {"baseline_file", "baseline_profile"}
    if invalid:
        raise ValueError("DeepEP 不接受 RCCL 基准参数：" + ", ".join(sorted(invalid)))
    if not request.tests:
        request.tests = ["intranode"] if request.group_size == 1 else ["lowl", "norm"]
    allowed = ("intranode", "lowl", "norm")
    if len(set(request.tests)) != len(request.tests) or any(item not in allowed for item in request.tests):
        raise ValueError("DeepEP tests 含未知项或存在重复项")
    errors = [unsupported_reason(request, mode) for mode in request.tests]
    if all(errors):
        raise ValueError("DeepEP 请求项全部不支持：\n" + "\n".join(errors))


def unsupported_reason(request: Request, mode: str) -> str:
    """返回单项不在当前支持范围内的原因；空串表示配置受支持。"""
    layout = f"group-size={request.group_size}, gpus-per-node={request.gpus_per_node}"
    if mode == "intranode":
        if request.group_size != 1 or request.gpus_per_node not in (2, 4, 8):
            return f"intranode 仅支持 group-size=1 且每节点 2/4/8 卡；当前 {layout}"
        return ""
    if request.group_size < 2 or request.gpus_per_node != 8:
        return f"{mode} 当前仅支持 group-size>1 且每节点 8 卡；当前 {layout}"
    count = request.group_size * request.gpus_per_node
    experts = 256 if mode == "norm" else 288
    if experts % count:
        return f"{mode} 总 GPU 数 {count} 必须整除专家数 {experts}；当前 {layout}"
    return ""


def stages(request: Request) -> list[str]:
    """返回有序准备阶段和一个或多个独立测试项。"""
    return ["prepare", *request.tests]
