#!/usr/bin/env python3
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""目标环境一次性预检：查询原生运行时可见卡数，不选卡、不写记录、不启动测试。

RCCL 查询 HIP；DeepEP/byte-flux 查询实际使用的 PyTorch。
继承 source 后的环境，不修改任何设备可见性变量。仅在组员准备阶段调用。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
from pathlib import Path
import sys


def hip_device_count() -> int:
    """在当前进程加载 HIP 并查询数量；不切换设备、不采集 PCI、不分配显存。"""
    libraries = [ctypes.util.find_library("amdhip64"), "libamdhip64.so"]
    for name in ("ROCM_PATH", "ROCM_HOME", "HIP_PATH", "DTK_ROOT", "DTK_HOME"):
        if os.environ.get(name):
            libraries.extend(str(Path(os.environ[name]) / directory / "libamdhip64.so")
                             for directory in ("lib", "lib64"))
    runtime = None
    for library in libraries:
        if library:
            try:
                runtime = ctypes.CDLL(library)
                break
            except OSError:
                continue
    if runtime is None:
        raise RuntimeError("HIP runtime libamdhip64.so is unavailable")
    runtime.hipInit.argtypes = [ctypes.c_uint]
    runtime.hipInit.restype = ctypes.c_int
    runtime.hipGetDeviceCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    runtime.hipGetDeviceCount.restype = ctypes.c_int
    code = runtime.hipInit(0)
    if code:
        raise RuntimeError(f"hipInit failed: HIP status={code}")
    count = ctypes.c_int()
    code = runtime.hipGetDeviceCount(ctypes.byref(count))
    if code:
        raise RuntimeError(f"hipGetDeviceCount failed: HIP status={code}")
    return count.value


def main() -> int:
    """确认测试所用运行时可用且可见卡数足够，日志随本节点准备结果保存。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--runtime", required=True, choices=("hip", "torch"))
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    if args.runtime == "hip":
        visible = hip_device_count()
    else:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch GPU runtime is unavailable")
        visible = torch.cuda.device_count()
    print(f"[GPU-CHECK] runtime={args.runtime} requested={args.count} visible={visible}", flush=True)
    if visible < args.count:
        raise RuntimeError(f"requested {args.count} GPUs; only {visible} are visible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ImportError, AttributeError) as error:
        print(f"[GPU-CHECK-ERROR] {error}", file=sys.stderr)
        raise SystemExit(69)
