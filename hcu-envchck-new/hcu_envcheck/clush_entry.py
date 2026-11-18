"""私有运行时内的 clush 入口：先还原用户环境，再启动 ClusterShell。

loader 已按启动时的 LD_LIBRARY_PATH 加载本进程；还原 os.environ 只影响其子进程，
ssh/scp 因此不会加载 runtime 内的 libssl/libcrypto。
"""
from pathlib import Path
import os
import resource
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hcu_envcheck.clush import subprocess_environment


def main() -> None:
    """放宽文件描述符软上限：每个 ssh 子进程占用多个 fd，千节点时默认 1024 不够。"""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    environment = subprocess_environment()
    os.environ.clear()
    os.environ.update(environment)
    from ClusterShell.CLI.Clush import main as clush
    clush()


if __name__ == "__main__":
    main()
