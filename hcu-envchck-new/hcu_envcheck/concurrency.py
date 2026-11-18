"""报告处理使用的有界并发接口，结果始终保持输入顺序。"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(function: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """等待全部任务结束才返回，不遗留仍写日志的后台工作线程。"""
    values = list(items)
    if not values:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(values)), thread_name_prefix="hcu") as pool:
        return list(pool.map(function, values))
