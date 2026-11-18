# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""报告共用的有限数值转换；不读取文件、不解释执行状态。"""
import math


def number(value, default=None):
    """仅接受有限浮点值，缺失或无效指标交由上层显式处理。"""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default
