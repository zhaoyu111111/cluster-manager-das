"""调度入口：clush 按计划逐批执行，全部结束后才调用 report。"""
from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
import secrets
import signal
import threading
import traceback

from .clush import Transport
from . import dispatch
from .models import Plan, Request
from .planning import write_plan
from .reporting.tables import atomic_text


def run(request: Request, plan: Plan, operation) -> int:
    """执行结束后关闭证据并生成报告；取消时保留已回收的结果。"""
    transport = Transport(request)
    transport.require()
    run_id = "hcu" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(5)
    parent = Path(request.output_dir or Path.cwd() / "verify_results").resolve()
    root = parent / f"{request.scenario}_{request.operation}_{run_id}"
    root.mkdir(mode=0o700, parents=True)
    write_plan(root, request, plan)
    atomic_text(root / "run.json", json.dumps(dict(run_id=run_id, state="RUNNING"), indent=2) + "\n")
    print(f"工作目录：{root}", flush=True)
    cancelled = threading.Event()
    old_handlers = {}

    def cancel(signum, frame):
        """信号只登记取消并停止 clush；远端残留用 idle-check --release 回收。"""
        if cancelled.is_set():
            return
        cancelled.set()
        transport.cancel_all()

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.signal(signum, cancel)
    try:
        dispatch.run(request, plan, operation, transport, root, run_id)
        # 此时全部 clush 已结束，报告可以读取稳定文件。
        atomic_text(root / ".state" / "execution.closed", f"{run_id}\n")
        atomic_text(root / "run.json", json.dumps(dict(run_id=run_id, state="CANCELLED" if cancelled.is_set() else "FINISHED"),
                                                  ensure_ascii=False, indent=2) + "\n")
        from .reporting.engine import generate
        try:
            code = generate(root, request, plan)
        except Exception as error:
            atomic_text(root / "report-error.log", traceback.format_exc())
            print(f"[REPORT-ERROR] {error}；原始证据：{root}", flush=True)
            code = 70
        if cancelled.is_set():
            print("[CANCELLED] 远端进程可能残留，请用 idle-check --release 回收", flush=True)
        return 130 if cancelled.is_set() else code
    finally:
        transport.cancel_all()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
