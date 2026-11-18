"""解析平台采集日志，调用原工程健康规则和静态一致性算法，发布节点报告。

采集退出码保存在 execution.tsv；本模块只写派生结果，不执行远端命令。
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from ..concurrency import parallel_map
from ..environment import evaluate_environment
from .consistency import (build_consistency_summary, build_platform_overview,
                          render_platform_markdown, render_platform_terminal)
from .tables import atomic_text, read_table, table_text

# 与原 reporting/parsers.py PLATFORM_PROFILE 一致：不虚构站点版本或新增必选依赖。
PLATFORM_PROFILE = dict(expected_device_count=None, require_compiler=False, require_rdma=False,
                        minimum_rdma_devices=0, require_rccl=False, require_ucx=False,
                        expected_rdma_protocol="auto", rdma_policy=None, required_python_packages=("torch",))


def parse_payload(text: str, token: str) -> dict | None:
    """只接受本 token 的一次完整采集区间，忽略 source 输出，不拿空 JSON 充当成功。"""
    lines = text.splitlines()
    begin = f"[PLATFORM-BEGIN] token={token}"
    end = f"[PLATFORM-END] token={token} exit_code=0"
    if lines.count(begin) != 1 or lines.count(end) != 1 or lines.index(begin) >= lines.index(end):
        return None
    payloads = []
    for line in lines[lines.index(begin) + 1:lines.index(end)]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("schema_version") == "1.0" and isinstance(item.get("system"), dict):
            payloads.append(item)
    return payloads[0] if len(payloads) == 1 else None


def parse_node(job: tuple[Path, dict, dict]) -> tuple[dict, dict]:
    """逐节点应用原平台健康规则，执行失败保持原因，健康结论只替换采集成功。"""
    root, fact, request = job
    result = dict(fact)
    path = root / fact["log"]
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    payload = parse_payload(text, fact["token"])
    checks = [dict(check_id="PAYLOAD_PARSE", status="PASS" if payload is not None else "FAIL",
                   message="platform schema=1.0" if payload is not None else "未取得本任务完整的平台采集 JSON")]
    summary, findings = {}, []
    state_file = root / ".state/platform" / f"{fact['target_id']}.json"
    metadata = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    if payload is not None:
        findings, summary, rule_checks = evaluate_environment(
            payload, network_host_scope_verified=metadata.get("network_host_scope_verified", False), **PLATFORM_PROFILE)
        checks.extend(rule_checks)
    if result["status"] == "PASS":
        if any(row["status"] == "FAIL" for row in checks):
            result.update(status="FAIL", reason_code="PLATFORM_HEALTH_FAILED", detail="平台健康规则存在 FAIL，详见 platform-checks.tsv")
        elif any(row["status"] in ("WARN", "UNKNOWN") for row in checks):
            result.update(status="WARN", reason_code="PLATFORM_HEALTH_WARN", detail="平台健康规则包含 WARN/UNKNOWN，详见 platform-checks.tsv")
        else:
            result.update(reason_code="PLATFORM_HEALTH_PASS", detail="平台健康检查通过")
    record = dict(node=fact["node"], target_id=fact["target_id"],
                  status={"PASS": "READY", "FAIL": "BLOCKED"}.get(result["status"], "INCOMPLETE"),
                  reachable=True if payload is not None else None, environment=summary,
                  findings=[asdict(item) for item in findings], checks=checks,
                  execution_exit_code=fact["exit_code"], execution_status=fact["status"],
                  execution_reason_code=fact["reason_code"], execution_detail=fact["detail"],
                  result_status=result["status"], evidence=fact["log"],
                  collection_scope=request["scenario"], collection=metadata)
    return result, record


def generate(root: Path) -> None:
    """复用公共报告并发池，输出所有节点规则、配置和一致性，不回写原始事实。"""
    request = json.loads((root / "plan/request.json").read_text(encoding="utf-8"))
    facts = read_table(root / "results.tsv")
    parsed = parallel_map(parse_node, [(root, fact, request) for fact in facts], int(request["concurrency"]))
    results, records = [item[0] for item in parsed], [item[1] for item in parsed]
    atomic_text(root / "results.tsv", table_text(list(facts[0]), results))
    checks = [dict(target_id=record["target_id"], node=record["node"], check=row["check_id"],
                   status=row["status"], detail=row["message"], exit_code=record["execution_exit_code"],
                   evidence=record["evidence"]) for record in records for row in record["checks"]]
    atomic_text(root / "platform-checks.tsv", table_text(
        ["target_id", "node", "check", "status", "detail", "exit_code", "evidence"], checks))
    findings = [dict(target_id=record["target_id"], node=record["node"], **item) for record in records for item in record["findings"]]
    atomic_text(root / "platform-findings.tsv", table_text(
        ["target_id", "node", "severity", "reason_code", "message", "device_id"], findings))
    atomic_text(root / "platform-nodes.json", json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    consistency = build_consistency_summary(records)
    atomic_text(root / "platform-consistency.json", json.dumps(consistency, ensure_ascii=False, indent=2) + "\n")
    overview = build_platform_overview(records, consistency)
    atomic_text(root / "platform-overview.json", json.dumps(overview, ensure_ascii=False, indent=2) + "\n")
    atomic_text(root / "platform-consistency.md", render_platform_markdown(overview))
    atomic_text(root / "platform-profile.json", json.dumps(PLATFORM_PROFILE, ensure_ascii=False, indent=2) + "\n")
    print(f"[PLATFORM-REPORT] nodes={len(records)} checks={len(checks)} consistency={consistency['comparison_status']}", flush=True)
    print(render_platform_terminal(overview), flush=True)
