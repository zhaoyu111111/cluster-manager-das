"""Linux 离线脚本夹具：原脚本复制到临时目录，外部执行命令使用固定替身。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from hcu_envcheck.dispatch import _config
from hcu_envcheck.planning import build_plan, write_plan
from hcu_envcheck.rccl_baseline import COLLECTIVES
from hcu_envcheck.registry import get_operation
from tests.helpers import PROJECT, RUN_ID

SHELL_AVAILABLE = sys.platform.startswith("linux") and shutil.which("bash") is not None and shutil.which("timeout") is not None


class PayloadJob:
    def __init__(self, workspace, operation="rccl", nodes=1, **changes):
        index = len(list(workspace.work.glob("payload-*")))
        self.root = workspace.work / f"payload-{index}"
        self.root.mkdir()
        self.bin = self.root / "stub-bin"
        self.bin.mkdir()
        self.hostfile = self.root / "hosts"
        self.hostfile.write_text("".join(f"node{i:03d}\n" for i in range(1, nodes + 1)))
        ssh_dir = self.root / "ssh-source"
        ssh_dir.mkdir()
        request_changes = dict(hostfile=str(self.hostfile), container_python="target-python", ssh_dir=str(ssh_dir))
        request_changes.update(changes)
        self.request = workspace.request(operation, **request_changes)
        if operation == "rccl" and not self.request.tests:
            self.request.tests = ["all_reduce"]
        selected = get_operation(operation)
        selected.validate(self.request)
        self.plan = build_plan(self.request, selected)
        write_plan(self.root, self.request, self.plan)
        self.target = self.plan.targets[0]
        self.payloads = self.root / "payloads"
        self.payloads.mkdir()
        for path in (PROJECT / "hcu_envcheck/payloads").iterdir():
            if path.is_file():
                (self.payloads / path.name).write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
        (self.root / "RUN_ID").write_text(RUN_ID + "\n")
        (self.root / "results").mkdir()
        config = _config(self.request, RUN_ID)
        import shlex
        config += f"HCU_RUN_DIR={shlex.quote(str(self.root))}\nHCU_WORKDIR={shlex.quote(str(self.root))}\n"
        (self.root / "config.env").write_text(config)
        for relative in ("source/DeepEP/tests", "source/flux/test/python"):
            (self.root / relative).mkdir(parents=True)
        source = (Path(__file__).parent / "stubs/command.py").read_text(encoding="utf-8")
        command = self.bin / "_command.py"
        command.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        command.chmod(0o700)
        for name in ("ssh", "docker", "mpirun", "hy-smi", "ss", "hostname", "sleep", "cat", "target-python",
                     *(name + "_perf" for name in COLLECTIVES)):
            (self.bin / name).symlink_to(command.name)
        self.state_path, self.calls_path = self.root / "stub-state.json", self.root / "stub-calls.jsonl"
        self.state_path.write_text("{}")
        self.calls_path.write_text("")
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            HCU_NODE=self.target.leader, HCU_TARGET=self.target.id, HCU_STUB_STATE=str(self.state_path),
            HCU_STUB_CALLS=str(self.calls_path), HCU_STUB_ROOT=str(self.root), MPIRUN_BIN=str(self.bin / "mpirun"),
            RCCL_BIN_DIR=str(self.bin))
        for key in ("PYTHONHOME", "PYTHONPATH", "LD_PRELOAD"):
            self.environment.pop(key, None)

    def configure(self, **values):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state.update(values)
        self.state_path.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def calls(self, command=None):
        rows = [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if command is None or row["command"] == command]

    def run(self, script, *arguments):
        return subprocess.run(["bash", str(self.payloads / script), *arguments], env=self.environment,
                              cwd=self.root, text=True, capture_output=True, timeout=30)

    def text(self, name):
        return (self.root / "results" / name).read_text(encoding="utf-8")
