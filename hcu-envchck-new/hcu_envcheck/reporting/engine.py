"""控制端报告入口：并发读取执行事实，调用专用解析器，统一发布展示结果。"""
from __future__ import annotations
from collections import Counter
import json
from pathlib import Path

from ..concurrency import parallel_map
from ..models import FACT_FIELDS, Plan, Request
from .tables import atomic_text, read_table, table_text


def read_target(job: tuple[Path, str, str, list[str]]) -> list[dict]:
    """读取一个目标的所有阶段，校验身份、退出码和日志完整性。"""
    root, target_id, node, stages = job
    output = []
    for stage in stages:
        path = root / ".state" / "facts" / stage / f"{target_id}.tsv"
        log = Path("logs") / stage / f"{target_id}.stdout.log"
        fallback = dict.fromkeys(FACT_FIELDS, "")
        fallback.update(target_id=target_id, node=node, stage=stage, status="INCOMPLETE",
                        reason_code="MISSING_FACT", exit_code=1, log=log.as_posix(),
                        detail="未取得有效执行事实或日志，不能判定通过")
        try:
            rows = read_table(path)
            if len(rows) != 1 or list(rows[0]) != FACT_FIELDS:
                raise ValueError("事实字段不完整")
            row = rows[0]
            if (row["target_id"], row["node"], row["stage"]) != (target_id, node, stage):
                raise ValueError("事实身份不匹配")
            if row["log"] != log.as_posix():
                raise ValueError("日志路径不匹配")
            for evidence in (path, root / log, (root / log).with_name(f"{target_id}.stderr.log")):
                if not evidence.is_file() or evidence.is_symlink():
                    raise ValueError("日志不存在或不是普通文件")
            if row["status"] not in {"PASS", "WARN", "FAIL", "TIMEOUT", "BLOCKED", "CANCELLED", "INCOMPLETE", "UNKNOWN"}:
                raise ValueError("状态无效")
            if row["status"] == "PASS" and (int(row["exit_code"]) != 0 or int(row["transport_exit_code"]) != 0):
                raise ValueError("通过结果包含非零退出码")
            output.append(row)
        except (OSError, ValueError, KeyError, TypeError) as error:
            if path.exists():
                fallback.update(status="FAIL", reason_code="INVALID_EVIDENCE", detail=str(error))
            output.append(fallback)
    return output


def markdown_cell(value: object) -> str:
    """转义单元格，避免原始日志破坏终端报告表格结构。"""
    return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def read_prepare_nodes(root: Path, plan: Plan) -> list[dict]:
    """读取已关闭的逐节点预检回执，直接展示原因和退出码，不跨节点推断或覆盖。"""
    rows = []
    for target in plan.targets:
        path = root / ".state" / "prepared" / f"{target.id}.json"
        if not path.is_file():
            continue
        prepared = json.loads(path.read_text(encoding="utf-8"))
        for member in prepared["members"]:
            result = member.get("prepare_result")
            # 旧回执或未派发节点没有原始退出码，不能从其他节点补造。
            if result is None:
                continue
            if result["node"] != member["node"] or result["node"] not in target.nodes:
                raise ValueError(f"{target.id} 逐节点预检身份不匹配")
            rows.append(dict(result, target_id=target.id,
                             log=f"logs/prepare/{target.id}.stdout.log"))
    if rows:
        fields = ["target_id", "node", "phase", "status", "reason_code", "exit_code",
                  "command_exit_code",
                  "detail", "log"]
        atomic_text(root / "prepare-nodes.tsv", table_text(fields, rows))
    return rows


def generate(root: Path, request: Request, plan: Plan) -> int:
    """执行关闭后读取所有组日志，最终一次加载基准并发布表格和JSON报告。"""
    if not (root / ".state" / "execution.closed").is_file():
        raise ValueError("执行尚未关闭，不能开始报告")
    jobs = [(root, target.id, target.leader, plan.stages) for target in plan.targets]
    parsed = parallel_map(read_target, jobs, request.concurrency)
    indexed = {(row["target_id"], row["stage"]): row for rows in parsed for row in rows}
    facts = [indexed[(target.id, stage)] for stage in plan.stages for target in plan.targets]
    atomic_text(root / "execution.tsv", table_text(FACT_FIELDS, facts))
    # 原始 execution.tsv 已发布；收集失败只影响派生结果，不改写实际命令退出码。
    for row in facts:
        try:
            receipt = json.loads((root / ".state/log-collection" / (row["target_id"] + ".json")).read_text(encoding="utf-8"))
            error = "" if receipt.get("status") == "PASS" else receipt.get("detail", "日志收集失败")
        except (OSError, ValueError, AttributeError) as problem:
            error = str(problem)
        if error and row["status"] == "PASS":
            row.update(status="FAIL", reason_code="LOG_COLLECTION_FAILED", detail=error)
    atomic_text(root / "results.tsv", table_text(FACT_FIELDS, facts))
    idle_report = None
    if request.operation == "idle-check":
        from .idle_report import collect
        idle_report = collect(root, request, plan, facts)
    elif request.operation == "platform":
        from .platform_report import generate as evaluate
        evaluate(root)
    elif request.operation == "checkdeepep":
        from .checkdeepep_report import generate as evaluate
        evaluate(root)
    elif request.operation == "rccl":
        from .rccl_report import generate as evaluate
        evaluate(root)
    elif request.operation == "byte-flux":
        from .byte_flux_report import generate as evaluate
        evaluate(root)
    elif request.operation == "deepep":
        from .deepep_report import generate as evaluate
        evaluate(root)
    facts = read_table(root / "results.tsv")
    prepare_nodes = read_prepare_nodes(root, plan) if "prepare" in plan.stages else []
    counts = Counter(row["status"] for row in facts)
    state = json.loads((root / "run.json").read_text(encoding="utf-8"))
    failed = any(row["status"] not in ("PASS", "WARN") for row in facts)
    status = "FAIL" if failed else "WARN" if counts["WARN"] else "PASS"
    code = 1 if failed else 2 if status == "WARN" else 0
    if request.operation == "idle-check" and state["state"] == "CANCELLED":
        code = 130
    lines = [f"# {request.operation} 检测报告", "", f"执行结果：**{status}**，退出码 {code}",
             f"目标环境：{request.scenario}" + (f" / {request.container}" if request.container else ""), ""]
    if request.operation != "platform":
        lines.extend([f"| 目标 | {'启动节点' if plan.grouped else '节点'} | 阶段 | 结果 | 原因 | 退出码 | 说明 |",
                      "| --- | --- | --- | --- | --- | --- | --- |"])
        for row in facts:
            lines.append("| " + " | ".join(markdown_cell(row[key]) for key in
                         ("target_id", "node", "stage", "status", "reason_code", "exit_code", "detail")) + " |")
    skipped_nodes = []
    if plan.skipped_nodes:
        reason = f"尾组仅 {len(plan.skipped_nodes)} 个节点，不足 group-size={request.group_size}"
        skipped_nodes = [dict(node=node, reason=reason) for node in plan.skipped_nodes]
        lines.extend(["", "## 跳过节点", "", "以下节点未执行预检或测试，不计入成功或失败统计。", "",
                      "| 节点 | 跳过原因 |", "| --- | --- |"])
        for row in skipped_nodes:
            lines.append(f"| {markdown_cell(row['node'])} | {markdown_cell(row['reason'])} |")
        print(f"[SKIP] 节点：{','.join(plan.skipped_nodes)}；原因：{reason}", flush=True)
    if prepare_nodes:
        lines.extend(["", "## 各节点预检", "", "逐节点原始判定见 [prepare-nodes.tsv](prepare-nodes.tsv)。空白退出码表示该命令未执行或未取得回执。", "",
                      "| 目标 | 节点 | 结果 | 原因 | 判定退出码 | 命令退出码 | 说明 |",
                      "| --- | --- | --- | --- | --- | --- | --- |"])
        for row in prepare_nodes:
            lines.append("| " + " | ".join(markdown_cell("" if row.get(key) is None else row[key]) for key in
                         ("target_id", "node", "status", "reason_code", "exit_code", "command_exit_code", "detail")) + " |")
            if row["status"] != "PASS":
                print(f"[PREPARE-NODE] target={row['target_id']} node={row['node']} status={row['status']} "
                      f"reason={row['reason_code']} exit_code={row['exit_code']} detail={row['detail']}", flush=True)
    for filename in ("rccl-summary.tsv", "deepep-summary.tsv", "byte-flux-summary.tsv", "checks.tsv"):
        if (root / filename).is_file():
            lines.extend(["", f"详细指标：[{filename}]({filename})"])
    if request.operation == "platform":
        lines.extend(["", (root / "platform-consistency.md").read_text(encoding="utf-8"),
                      "", "## 查看原始明细", "", "分组总览：[platform-overview.json](platform-overview.json)；完整执行明细：[results.tsv](results.tsv)。",
                      "", "平台规则明细：[platform-checks.tsv](platform-checks.tsv)；问题原因：[platform-findings.tsv](platform-findings.tsv)。",
                      "", "节点配置：[platform-nodes.json](platform-nodes.json)；采集退出 0 只代表采集完成，不等于健康通过。"])
    if idle_report is not None:
        from .idle_report import markdown, terminal
        lines.extend(markdown(idle_report))
        print(terminal(idle_report), flush=True)
    lines.extend(["", "原始执行事实见 `.state/facts/`；原始输出见 `logs/`。", ""])
    atomic_text(root / "summary.md", "\n".join(lines))
    atomic_text(root / "report-status.tsv", table_text(["status", "exit_code", "pass", "warn", "fail"],
                [dict(status=status, exit_code=code, **{"pass": counts["PASS"], "warn": counts["WARN"],
                                                     "fail": sum(v for k, v in counts.items() if k not in ("PASS", "WARN"))})]))
    atomic_text(root / "results.json", json.dumps(dict(status=status, exit_code=code, counts=dict(counts),
                                                       operation=request.operation, results=facts,
                                                       prepare_nodes=prepare_nodes,
                                                       skipped_nodes=skipped_nodes,
                                                       **({"idle_check": idle_report} if idle_report is not None else {})), ensure_ascii=False, indent=2) + "\n")
    print(f"[RESULT] {status}；PASS={counts['PASS']} WARN={counts['WARN']}；报告：{root / 'summary.md'}", flush=True)
    return code
