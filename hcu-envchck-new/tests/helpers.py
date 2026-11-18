"""仅供测试使用的数据工厂和传输替身，业务模块使用真实实现。"""
from __future__ import annotations

import base64
from dataclasses import asdict
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest

from hcu_envcheck.models import NodeResult, Request, TaskResult
from hcu_envcheck.reporting.tables import atomic_text

PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
RUN_ID = "hcu_unit_run"
CID = "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class Workspace(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hcu-unit-")
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.hostfile = self.work / "hosts"
        self.hostfile.write_text("node001\nnode002\n", encoding="utf-8")
        self.root = self.work / "run"
        self.root.mkdir()

    def request(self, operation="rccl", **changes):
        values = dict(operation=operation, hostfile=str(self.hostfile), root=str(PROJECT),
                      container="unit-container", image="unit-image:1", output_dir=str(self.work / "output"))
        values.update(changes)
        return Request(**values)

    def hosts(self, count):
        nodes = [f"node{i:04d}" for i in range(1, count + 1)]
        self.hostfile.write_text("\n".join(nodes) + "\n", encoding="utf-8")
        return nodes


def fact(target="group-001", node="node001", stage="all_reduce", code=0, **changes):
    values = dict(target_id=target, node=node, stage=stage,
                  status="PASS" if code == 0 else "FAIL", exit_code=code,
                  transport_exit_code=code, token=f"{RUN_ID}_{target}_{stage}",
                  reason_code="EXECUTION_OK" if code == 0 else "RCCL_EXECUTION_FAILED",
                  started_epoch=10, ended_epoch=11, detail="unit evidence")
    values.update(changes)
    return TaskResult(**values)


def fact_row(**changes):
    return {key: str(value) for key, value in asdict(fact(**changes)).items()
            if key not in {"stdout", "stderr"}}


def section(stage, body="", code=0, target="group-001", token=None):
    token = token or f"{RUN_ID}_{target}_{stage}"
    return (f"[TEST-BEGIN] test={stage} token={token}\n{body}\n"
            f"[TEST-END] test={stage} token={token} exit_code={code}\n")


def idle_sample(vram=0, util=0, count=2, code=0, node="node001", raw=None):
    raw = raw if raw is not None else (
        "Card Temp Power Clock Fan VRAM% HCU%\n" +
        "".join(f"{index} 30 20 100 0 {vram}% {util}%\n" for index in range(count)))
    encoded = base64.b64encode(raw.encode()).decode()
    return (f"HCU_IDLE_BEGIN\t1\nHCU_IDLE_META\t{node}\thost\t100\n"
            f"HCU_IDLE_RAW\tmetrics\t{code}\t{encoded}\nHCU_IDLE_END\t1\n")


def node_result(root, node="node001", status="PASS", reason="PREPARE_READY", code=0, files=None):
    directory = root / node / "results"
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        atomic_text(directory / name, content)
    return NodeResult(node, directory, status, reason, code, "unit evidence")


def platform_record(node="node001", **changes):
    device = dict(model="TEST_CARD", architecture="gfx-test", compute_units=80, memory_bytes=64 * 1024**3)
    record = dict(node=node, reachable=True, execution_status="PASS", status="READY",
                  environment=dict(container_os="Test Linux", kernel="test-kernel", python_version="3.12",
                                   driver_version="driver-1", dtk_version="dtk-1", torch_version="torch-1",
                                   python_packages={"torch": "torch-1"}, transformer_engine_version="te-1",
                                   deepep_version="ep-1", hcu_devices=[device.copy() for _ in range(8)],
                                   hcu_hardware_status="COLLECTED", mem_total="1024 kB", dev_shm={"total_bytes": 100}),
                  checks=[dict(check_id=key, status="PASS", message="unit healthy") for key in
                          ("HYCU_DRIVER_MODULE", "HCU_DEVICE_NODE", "TORCH_HCU_AVAILABLE", "RDMA_DEVICE_COUNT")])
    record.update(changes)
    return record


class FakeTransport:
    """只写预设的节点结果包；记录 fanout/copy/rcopy 顺序，不创建子进程。"""
    def __init__(self, packages=None):
        self.packages = packages or {}
        self.calls = []
        self.cancelled = threading.Event()

    def require(self):
        self.calls.append(("require",))

    def fanout(self, hosts, command, limit, log):
        self.calls.append(("fanout", Path(hosts).read_text(encoding="utf-8").splitlines(), command))
        return 0

    def copy(self, hosts, source, destination, limit, log):
        self.calls.append(("copy", Path(hosts).read_text(encoding="utf-8").splitlines(), destination))
        return 0

    def rcopy(self, hosts, source, destination, limit, log):
        nodes = Path(hosts).read_text(encoding="utf-8").splitlines()
        mode = Path(destination).name
        self.calls.append(("rcopy", nodes, mode))
        for node in nodes:
            files = self.packages.get((mode, node))
            if files is None:
                continue
            package = Path(destination) / f"out.tgz.{node}"
            package.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(package, "w:gz") as archive:
                for name, content in files.items():
                    data = content.encode() if isinstance(content, str) else content
                    info = tarfile.TarInfo("results/" + name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        return 0

    def cancel_all(self):
        self.cancelled.set()
        self.calls.append(("cancel",))


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False) + "\n")
