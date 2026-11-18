"""ClusterShell 传输及本地子进程生命周期；不判断具体检测是否通过。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading

from .models import Request


def subprocess_environment() -> dict[str, str]:
    """恢复启动器修改前的环境，防止私有控制 Python 污染 ssh/scp 及远端。"""
    environment = dict(os.environ)
    prefix = "HCU_LAUNCH_ORIGINAL_"
    for name in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "PATH", "VIRTUAL_ENV",
                 "CONDA_PREFIX", "CONDA_DEFAULT_ENV"):
        state = environment.get(f"{prefix}{name}_SET")
        if state == "0":
            environment.pop(name, None)
        elif state == "1":
            environment[name] = environment.get(prefix + name, "")
    return {key: value for key, value in environment.items() if not key.startswith(prefix)}


class Transport:
    """通过同一 clush 批量执行、分发和回收，并管理控制端子进程。"""

    def __init__(self, request: Request):
        """固定请求，注册运行中的 clush 进程以便取消。"""
        self.request = request
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.cancelled = threading.Event()

    def require(self) -> None:
        """执行前确认私有运行时已包含 ClusterShell。"""
        if importlib.util.find_spec("ClusterShell") is None:
            raise ValueError("控制端 Python 缺少 ClusterShell；请使用 runtime 离线发行包")

    def _base(self) -> list[str]:
        """组装公共 clush 参数；端口用 -o Port，ssh 和 scp 含义相同。"""
        req = self.request
        options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={req.connect_timeout}",
                   "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                   "-o", f"StrictHostKeyChecking={req.ssh_strict}", "-o", f"Port={req.ssh_port}"]
        if req.ssh_config:
            options += ["-F", req.ssh_config]
        argv = [sys.executable, "-B", "-s", "-m", "hcu_envcheck.clush_entry", "-o", shlex.join(options)]
        if req.ssh_user:
            argv += ["-l", req.ssh_user]
        return argv

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        """终止本次创建的进程组；远端残留由 idle-check --release 回收。"""
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    def cancel_all(self) -> None:
        """取消当前运行中的 clush，之后仍允许收集日志。"""
        with self._lock:
            self.cancelled.set()
            processes = tuple(self._processes)
        for process in processes:
            self._stop(process)

    def _spawn(self, argv: list[str], allow_cancelled: bool, **kwargs) -> subprocess.Popen | None:
        """取消后不再启动新任务；登记与启动在同一把锁内，避免漏杀。"""
        with self._lock:
            if self.cancelled.is_set() and not allow_cancelled:
                return None
            process = subprocess.Popen(argv, start_new_session=True, **kwargs)
            self._processes.add(process)
            return process

    def _fanout(self, hosts: Path, arguments: list[str], limit: int, log: Path,
                allow_cancelled: bool) -> int:
        """全部节点一条 clush 命令；完整命令写入日志，可复制后手工重跑。"""
        req = self.request
        argv = [*self._base(), "--hostfile", str(hosts), "-f", str(req.concurrency),
                "-t", str(req.connect_timeout), "-u", str(limit), "--nostdin", *arguments]
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as output:
            output.write("$ hcu-cluster-run clush " + shlex.join(argv[5:]) + "\n")
            output.flush()
            process = self._spawn(argv, allow_cancelled, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=subprocess.STDOUT)
            if process is None:
                return 130
            try:
                process.wait()
            finally:
                with self._lock:
                    self._processes.discard(process)
        return process.returncode

    def fanout(self, hosts: Path, command: str, limit: int, log: Path) -> int:
        """并发执行一条命令，输出按相同内容折叠；节点结论以回传状态文件为准。"""
        return self._fanout(hosts, ["-b", command], limit, log, False)

    def copy(self, hosts: Path, source: Path, destination: str, limit: int, log: Path) -> int:
        """分发单个文件到全部节点已存在的目录。"""
        return self._fanout(hosts, ["--copy", str(source), "--dest", destination], limit, log, False)

    def rcopy(self, hosts: Path, source: str, destination: Path, limit: int, log: Path) -> int:
        """收集单个文件，clush 在本地文件名后追加节点名；取消后仍可收集。"""
        return self._fanout(hosts, ["--rcopy", source, "--dest", str(destination)], limit, log, True)
