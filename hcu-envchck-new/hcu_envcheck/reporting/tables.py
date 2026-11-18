# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""报告共用的 TSV 读取、序列化和原子发布；不执行测试或判断业务状态。"""
from __future__ import annotations
import csv
import io
import os
from pathlib import Path

def read_table(path: Path) -> list[dict[str, str]]:
    """读取本次运行已经关闭的计划或派生表，不回写原始事实。"""
    with path.open(encoding='utf-8', newline='') as source:
        return list(csv.DictReader(source, delimiter='\t'))


def atomic_text(path: Path, text: str) -> None:
    """先写同目录临时文件，再发布一份完整结果，避免报告读取半张表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.tmp.{os.getpid()}')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def table_text(fields: list[str], records: list[dict]) -> str:
    """生成没有嵌入换行和制表符的 TSV，维持公共报告引擎的行格式。"""
    output = io.StringIO()
    writer = csv.DictWriter(output, fields, delimiter='\t', lineterminator='\n', extrasaction='ignore')
    writer.writeheader()
    for record in records:
        writer.writerow({key: '' if value is None else str(value).replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
                         for key, value in record.items() if key in fields})
    return output.getvalue()


