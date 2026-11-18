"""读取已收集的一份分组日志，按测试名/token 切分；不生成执行事实或补造结束记录。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def read_group(root: Path, group_id: str) -> tuple[dict, str]:
    """单组只读一次日志；校验收集回执和哈希，保留未结束/重复/错配的测试段。"""
    sections = {}
    try:
        receipt = json.loads((root / ".state/log-collection" / (group_id + ".json")).read_text(encoding="utf-8"))
        problem = "" if receipt.get("status") == "PASS" else receipt.get("detail", "分组日志未收集成功")
        if not receipt.get("sha256"):
            return sections, problem or "分组日志缺少收集哈希"
        data = (root / "logs/groups" / (group_id + ".log")).read_bytes()
        if hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
            return sections, "分组日志哈希与收集回执不一致"
        active = None
        for line in data.decode("utf-8", errors="replace").splitlines(keepends=True):
            begin = re.fullmatch(r"\[TEST-BEGIN\] test=(\S+) token=(\S+)\s*", line)
            end = re.fullmatch(r"\[TEST-END\] test=(\S+) token=(\S+) exit_code=(\d+)\s*", line)
            if begin:
                key = begin.groups()
                if active is not None:
                    sections[active]["error"] = "上一测试缺少结束标记"
                if key in sections:
                    sections[key]["error"] = "测试开始标记重复"
                else:
                    sections[key] = dict(text=[], exit_code=None, error="测试缺少结束标记")
                active = key
            elif end:
                key = end.group(1), end.group(2)
                if key != active:
                    if key in sections:
                        sections[key]["error"] = "结束标记重复或没有对应的活动测试"
                    else:
                        problem = "日志包含没有开始记录的结束标记"
                    if active is not None:
                        sections[active]["error"] = "测试结束标记身份不匹配"
                    active = None
                else:
                    section = sections[key]
                    if section["error"] == "测试缺少结束标记":
                        section["error"] = ""
                    section["exit_code"] = int(end.group(3))
                    active = None
            elif active is not None:
                sections[active]["text"].append(line)
        for section in sections.values():
            section["text"] = "".join(section["text"])
        return sections, problem
    except (OSError, ValueError, TypeError) as error:
        return sections, str(error)


def read_test(sections: dict, group_error: str, fact: dict) -> tuple[str, str]:
    """按真实执行 token 查找单项，只对完整且退出码一致的日志提供通过证据。"""
    section = sections.get((fact["stage"], fact["token"]))
    if section is None:
        return "", group_error or "缺少本次测试的分组日志段"
    error = group_error or section["error"]
    if not error and section["exit_code"] != int(fact["transport_exit_code"]):
        error = "日志退出码与实际执行退出码不一致"
    return section["text"], error
