# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""原平台规则的命令证据与问题模型；与新工程调度事实模型分开。"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Finding:
    severity: str
    reason_code: str
    message: str
    device_id: int | None = None
