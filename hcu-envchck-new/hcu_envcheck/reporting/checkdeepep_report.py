"""解析原子 DeepEP 环境检查的四段状态；不修改原脚本或执行环境。"""
from collections import Counter
from pathlib import Path
import re
from .tables import atomic_text, read_table, table_text


def generate(root: Path) -> None:
    """解释原脚本四段状态并核对退出码；保留原执行失败和未执行状态。"""
    facts = read_table(root / "results.tsv")
    names = ("ACS", "XDP", "RDMA", "ibv_devinfo")
    checks = []
    for fact in facts:
        path = root / fact["log"]
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        sections = {index: [] for index in range(1, 5)}
        seen = Counter()
        section = 0
        for line in text.splitlines():
            match = re.match(r"\s*====\s*([1-4])\.", line)
            if match:
                section = int(match[1])
                seen[section] += 1
            elif "====" in line:
                section = 0
            elif section and re.search(r"\[(PASS|WARN|FAIL)\]", line):
                sections[section].append(line.strip())
        statuses = []
        for index, name in enumerate(names, 1):
            messages = sections[index]
            status = ("UNKNOWN" if seen[index] != 1 or not messages else
                      "FAIL" if any("[FAIL]" in line for line in messages) else
                      "WARN" if any("[WARN]" in line for line in messages) else "PASS")
            statuses.append(status)
            checks.append(dict(node=fact["node"], check=name, status=status,
                               detail="; ".join(messages) or "原始检查段或状态缺失", evidence=fact["log"]))
        if fact["reason_code"] not in {"CHECK_OK", "CHECK_WARN", "CHECK_FAILED"}:
            continue
        if "UNKNOWN" in statuses:
            fact.update(status="FAIL", reason_code="CHECK_OUTPUT_INCOMPLETE", detail="原始检查四段输出不完整")
        else:
            expected = 1 if "FAIL" in statuses else 2 if "WARN" in statuses else 0
            if int(fact["exit_code"]) != expected:
                fact.update(status="FAIL", reason_code="CHECK_OUTPUT_CONFLICT", detail="原始状态与脚本退出码不一致")
    atomic_text(root / "checks.tsv", table_text(["node", "check", "status", "detail", "evidence"], checks))
    atomic_text(root / "results.tsv", table_text(list(facts[0]), facts))
