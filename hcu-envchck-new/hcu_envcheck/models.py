"""统一请求、执行计划与事实模型；不连接节点，也不包含测试判定。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ACTIVE_OPERATIONS = ("rccl", "deepep", "byte-flux")


@dataclass
class Request:
    """一次 CLI 请求。路径的控制端和目标端语义由参数名明确区分。"""
    operation: str
    hostfile: str
    root: str
    scenario: str = "per-node-container"
    container: str = ""
    image: str = ""
    output_dir: str = ""
    group_size: int = 1
    gpus_per_node: int = 8
    tests: list[str] = field(default_factory=list)
    concurrency: int = 1024
    timeout: int = 1800
    connect_timeout: int = 15
    ssh_user: str = ""
    ssh_config: str = ""
    ssh_port: int = 22
    ssh_strict: str = "yes"
    container_ssh_port: int = 25901
    env_script: str = ""
    container_workdir: str = ""
    container_python: str = "python3"
    remote_workdir: str = "/tmp/hcu-envcheck"
    baseline_file: str = ""
    baseline_profile: str = ""
    deepep_source: str = ""
    release: bool = False
    skip_idle_check: bool = False
    dry_run: bool = False
    yes: bool = False
    port: int = 25901
    volumes: list[str] = field(default_factory=list)
    docker_args: list[str] = field(default_factory=list)
    ssh_dir: str = ""
    container_user: str = ""
    dependency_manifest: str = ""
    flux_source: str = ""
    image_tar: str = ""
    provided: set[str] = field(default_factory=set)


def is_container(request: Request) -> bool:
    """判断目标是否需要 Docker；两种 Conda 场景使用相同的宿主执行方式。"""
    return request.scenario == "per-node-container"


def peer_port(request: Request) -> int:
    """容器使用容器 SSH 端口，Conda 使用宿主 SSH 端口。"""
    return request.container_ssh_port if is_container(request) else request.ssh_port


@dataclass(frozen=True)
class Target:
    """一个执行目标：普通检查为单节点，性能测试为一组节点。"""
    id: str
    nodes: tuple[str, ...]

    @property
    def leader(self) -> str:
        """返回 hostfile 顺序中的首节点。"""
        return self.nodes[0]


@dataclass
class Plan:
    """控制端生成的有序阶段和执行目标。"""
    targets: list[Target]
    stages: list[str]
    grouped: bool = False
    skipped_nodes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NodeResult:
    """一个节点回传的状态文件内容及结果目录；无回传时为失败状态。"""
    node: str
    results: Path
    status: str
    reason: str
    exit_code: int
    detail: str

    def text(self, name: str) -> str:
        """读取回传的结果文件，缺失时返回空证据。"""
        path = self.results / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


FACT_FIELDS = ["target_id", "node", "stage", "status", "reason_code", "exit_code",
               "container_id", "image_id", "detail", "started_epoch", "ended_epoch",
               "log", "token", "transport_exit_code"]


@dataclass
class TaskResult:
    """执行事实。性能阈值、日志解析结论由 reporting 另行生成。"""
    target_id: str
    node: str
    stage: str
    status: str = "PASS"
    reason_code: str = "EXECUTION_OK"
    exit_code: int = 0
    container_id: str = ""
    image_id: str = ""
    detail: str = ""
    # 统一由调度层赋值；模型和操作不各自重复计时。
    started_epoch: float = 0
    ended_epoch: float = 0
    log: str = ""
    token: str = ""
    transport_exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
