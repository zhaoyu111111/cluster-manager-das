# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""宿主空闲操作：采样与释放在目标端 idle.sh，本模块解析证据并给出最终判断。

Shell 判断只决定是否进入释放；结论以 reporting/idle.py 对原始采样的解析为准。
无 sudo、自动提权、容器删除或宿主进程强杀。
"""
from __future__ import annotations

import json

from ..models import NodeResult, TaskResult
from ..reporting.idle import evaluate_idle, parse_process_sample, parse_sample
from ..reporting.tables import atomic_text


def validate(request) -> None:
    """三种场景入口均可，实际检测宿主机；拒绝容器、环境和测试参数混入。"""
    allowed = {"hostfile", "output_dir", "concurrency", "timeout", "connect_timeout",
               "ssh_user", "ssh_port", "ssh_config", "ssh_strict", "release", "dry_run",
               "remote_workdir"}
    invalid = request.provided - allowed
    if invalid:
        raise ValueError("idle-check 是宿主操作，不接受参数：" + ", ".join(sorted(invalid)))
    if not 1 <= request.timeout <= 86400:
        raise ValueError("idle-check --timeout 应为 1～86400 秒，作用于每节点完整工作流")


def stages(request) -> list[str]:
    """每节点一个原子工作流。"""
    return ["idle-check"]


def _envelopes(text: str) -> list[str]:
    """按 BEGIN/END 切出每次采样，循环复检时日志中有多份。"""
    blocks, current = [], None
    for line in text.splitlines():
        if line.startswith("HCU_IDLE_BEGIN\t"):
            current = []
        if current is not None:
            current.append(line)
            if line.startswith("HCU_IDLE_END\t"):
                blocks.append("\n".join(current))
                current = None
    return blocks


def _record(node: NodeResult, release: bool) -> dict:
    """把原始日志还原成逐节点证据；缺失采样时判为 UNKNOWN。"""
    text = node.text("idle.log")
    history = []
    for block in _envelopes(text):
        if "HCU_IDLE_RAW\tshowpid" in block:
            if history:
                history[-1]["process_sample"] = parse_process_sample(block, node=node.node, scope="host")
            continue
        evidence = parse_sample(block, node=node.node, scope="host")
        history.append(dict(evidence=evidence, decision=evaluate_idle(evidence, operation="idle-check")))
    owners, actions, errors = [], [], []
    for line in text.splitlines():
        cells = line.split("\t")
        if cells[0] == "OWNER" and len(cells) == 6:
            owners.append(dict(pid=int(cells[1]), container_id=cells[2], container=cells[3],
                               started_at=cells[4], pid_start=cells[5]))
        elif cells[0] == "RELEASE" and len(cells) == 4:
            actions.append(dict(container_id=cells[1], container=cells[2], status=cells[3]))
        elif cells[0] in ("UNKNOWN", "EXITED") and len(cells) == 2:
            errors.append(f"PID {cells[1]} 归属不明，未停止")
        elif cells[0] == "IDLE_ERROR" and len(cells) == 2:
            errors.append(cells[1])
    unknown = dict(status="UNKNOWN", detail=node.detail, cards=[], busy_cards=[])
    decision = history[-1]["decision"] if history else unknown
    initial = history[0]["decision"] if history else unknown
    action_status = "NOT_REQUESTED"
    if release and initial["status"] == "BUSY":
        action_status = "CONFIRMED" if decision["status"] == "IDLE" and not errors else "UNCONFIRMED"
    record = dict(node=node.node, api="check_idle", scope="host", operation="idle-check", history=history,
                  decision=decision, initial_decision=initial, owners=owners, actions=actions, rechecks=[],
                  commands=[], release_requested=release, action_status=action_status,
                  exit_code={"IDLE": 0, "BUSY": 2, "UNKNOWN": 1}[decision["status"]])
    if errors:
        record["error"] = "；".join(errors)
    return record


def interpret(node: NodeResult, target, stage: str, root, run_id: str) -> TaskResult:
    """发布逐节点证据和执行事实，最终展示由 report 完成。"""
    release = json.loads((root / "plan/request.json").read_text(encoding="utf-8"))["release"]
    record = _record(node, release)
    status = record["decision"]["status"]
    passed = status == "IDLE" and record["action_status"] != "UNCONFIRMED"
    if status == "IDLE" and not passed:
        record["exit_code"] = 1
    reason = ("HOST_IDLE" if passed else "RELEASE_UNCONFIRMED" if record["action_status"] == "UNCONFIRMED"
              else "HOST_NOT_IDLE" if status == "BUSY" else "IDLE_EVIDENCE_INVALID")
    record.update(status="PASS" if passed else "FAIL", reason_code=reason)
    atomic_text(root / "idle" / f"{target.id}.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return TaskResult(target.id, node.node, stage, record["status"], reason, record["exit_code"],
                      detail=record.get("error") or record["decision"]["detail"], stdout=node.text("idle.log"),
                      transport_exit_code=record["exit_code"])
