# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""空闲检测的终端、JSON、TSV 及 Markdown 展示；只读取执行结束后的事实。

保留首次判断、复检指标、进程归属、释放回执及原始日志；不重新采样，不发起动作。
"""
from __future__ import annotations

import json
from pathlib import Path

from .tables import atomic_text, table_text


def collect(root: Path, request, plan, facts: list[dict]) -> dict:
    """读取公共并发调度已关闭的逐节点回执，缺失证据不能作为空闲通过。"""
    records = []
    for target, fact in zip(plan.targets, facts):
        try:
            record = json.loads((root / "idle" / f"{target.id}.json").read_text(encoding="utf-8"))
            if record["node"] != target.leader or record["decision"]["status"] not in {"IDLE", "BUSY", "UNKNOWN"}:
                raise ValueError("节点或判断身份不匹配")
            if fact["status"] == "CANCELLED":
                record.update(execution_exit_code=record["exit_code"], exit_code=int(fact["exit_code"]), cancelled=True)
            if int(record["exit_code"]) != int(fact["exit_code"]):
                raise ValueError("节点回执与执行事实退出码不一致")
            if fact["status"] == "PASS" and (record["decision"]["status"] != "IDLE"
                                             or record["action_status"] == "UNCONFIRMED"):
                raise ValueError("通过事实缺少空闲或释放确认")
        except (OSError, ValueError, KeyError, TypeError) as error:
            record = dict(node=target.leader, decision=dict(status="UNKNOWN", detail=str(error), cards=[]),
                          history=[], owners=[], actions=[], rechecks=[], commands=[],
                          exit_code=int(fact["exit_code"]) if fact["status"] in {"CANCELLED", "BLOCKED"} else 1,
                          action_status="UNCONFIRMED" if request.release else "NOT_REQUESTED", error=str(error))
            if fact["status"] not in {"CANCELLED", "BLOCKED"}:
                fact.update(status="FAIL", reason_code="INVALID_IDLE_EVIDENCE", exit_code=1, detail=str(error))
        record["evidence"] = fact["log"]
        records.append(record)
    initial = [record.get("initial_decision", record["decision"])["status"] for record in records]
    state = json.loads((root / "run.json").read_text(encoding="utf-8"))
    report = dict(schema_version=1, operation="idle-check", run_dir=str(root), nodes=records,
                  release_requested=request.release, cancelled=state["state"] == "CANCELLED",
                  status="PASS" if all(row["status"] == "PASS" for row in facts) else "FAIL",
                  check_result=dict(api="check_idle", complete="UNKNOWN" not in initial,
                                    status="UNKNOWN" if "UNKNOWN" in initial else "BUSY" if "BUSY" in initial else "IDLE"))
    if report["cancelled"]:
        report["check_result"]["complete"] = False
    atomic_text(root / "results.tsv", table_text(list(facts[0]), facts))
    atomic_text(root / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    rows = [dict(node=r["node"], status=r["decision"]["status"], exit_code=r["exit_code"],
                 action_status=r["action_status"], evidence=r["evidence"],
                 detail=r.get("error") or r["decision"]["detail"]) for r in records]
    atomic_text(root / "idle-results.tsv", table_text(
        ["node", "status", "exit_code", "action_status", "detail", "evidence"], rows))
    for target, record in zip(plan.targets, records):
        atomic_text(root / "idle" / f"{target.id}.rc", str(record["exit_code"]) + "\n")
    return report


def totals(report: dict) -> dict:
    """统计最终状态、失败节点及释放未确认数量，节点顺序与 hostfile 一致。"""
    nodes = report["nodes"]
    return {key: sum(row["decision"]["status"] == key for row in nodes) for key in ("IDLE", "BUSY", "UNKNOWN")} | {
        "total": len(nodes), "failed": sum(row["exit_code"] != 0 for row in nodes),
        "unconfirmed": sum(row["action_status"] == "UNCONFIRMED" for row in nodes)}


def terminal(report: dict) -> str:
    """显示每节点判断、繁忙卡、占用容器及释放结果，不执行采样或变更环境。"""
    lines = [f"[IDLE-CHECK] scope=host release={int(report['release_requested'])}"]
    for row in report["nodes"]:
        decision = row["decision"]
        # 千节点时只逐行显示需要处理的节点，完整结果见 idle-results.tsv。
        if decision["status"] == "IDLE" and not row["actions"] and not row.get("error"):
            continue
        lines.append(f"[{decision['status']}] node={row['node']} exit_code={row['exit_code']} {decision['detail']}")
        for card in decision.get("busy_cards", []):
            lines.append(f"[CARD] node={row['node']} card={card['card']} VRAM={card['vram_pct']:g}% HCU={card['util_pct']:g}%")
        for owner in row["owners"]:
            lines.append(f"[OWNER] node={row['node']} pid={owner['pid']} container={owner['container']} id={owner['container_id']}")
        for action in row["actions"]:
            lines.append(f"[RELEASE] node={row['node']} container={action['container']} status={action['status']}")
        if row.get("error"):
            lines.append(f"[ERROR] node={row['node']} {row['error']}")
    lines.append("[IDLE-SUMMARY] " + " ".join(f"{key.lower()}={value}" for key, value in totals(report).items()))
    return "\n".join(lines)


def markdown(report: dict) -> list[str]:
    """生成初始/最终判断、逐卡采样历史、归属及动作表，供公共 summary.md 合并。"""
    from .engine import markdown_cell

    def table(headers, rows):
        """转义所有字段并生成平面表格，避免容器名称或原始错误破坏报告。"""
        return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"] + [
            "| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in rows]

    lines = ["", "## 宿主机空闲检测", "", "标准：宿主机全部卡 VRAM%=0 且 HCU%=0；证据缺失或无效为 UNKNOWN。",
             f"初始判断：{report['check_result']['status']}；释放请求：{report['release_requested']}；取消：{report['cancelled']}。",
             "；".join(f"{key}={value}" for key, value in totals(report).items()), ""]
    lines += table(["节点", "首次", "最终", "退出码", "释放状态", "说明"], [
        [r["node"], r.get("initial_decision", r["decision"])["status"], r["decision"]["status"], r["exit_code"],
         r["action_status"], r.get("error") or r["decision"]["detail"]] for r in report["nodes"]])
    lines += ["", "### 采样及复检指标", ""]
    lines += table(["节点", "采样序号", "时间戳", "卡", "VRAM%", "HCU%", "判断"], [
        [r["node"], index, h["decision"]["sampled_at"], card["card"], card["vram_pct"], card["util_pct"], h["decision"]["status"]]
        for r in report["nodes"] for index, h in enumerate(r["history"], 1) for card in h["decision"]["cards"]])
    lines += ["", "### 占用归属与释放", ""]
    lines += table(["节点", "PID", "PID启动标识", "容器", "容器完整ID", "容器启动时间"], [
        [r["node"], o["pid"], o["pid_start"], o["container"], o["container_id"], o["started_at"]]
        for r in report["nodes"] for o in r["owners"]])
    lines += [""] + table(["节点", "容器", "容器完整ID", "停止确认"], [
        [r["node"], a["container"], a["container_id"], a["status"]] for r in report["nodes"] for a in r["actions"]])
    lines += ["", "完整证据：[report.json](report.json)；节点摘要：[idle-results.tsv](idle-results.tsv)。",
              "逐节点证据保存在 `idle/`，原始采样及释放回执保存在 `logs/idle-check/`。",
              "空闲采样不代表资源预留；未知、取消或释放未确认均不计成功。"]
    return lines
