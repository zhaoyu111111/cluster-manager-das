"""组优先的集群执行：clush 分发 → 节点/组执行 → 回收 → 发布执行事实。

组首节点按计划逐批执行；组员预检失败时整组测试记为 BLOCKED，其余组和后续批次继续。
节点结论只来自回传的状态文件，无回传的节点记为 NODE_NO_RESULT。
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

from .dependencies import source_snapshot
from .models import (ACTIVE_OPERATIONS, FACT_FIELDS, NodeResult, Plan, Request, Target, TaskResult,
                     is_container, peer_port)
from .reporting.group_log import read_group
from .reporting.tables import atomic_text, read_table, table_text

CHECK_TIMEOUT = 600
COPY_TIMEOUT = 600


def save_fact(root: Path, result: TaskResult) -> None:
    """发布单个目标阶段的日志和事实；事实不会被最终报告覆盖。"""
    relative = Path("logs") / result.stage / f"{result.target_id}.stdout.log"
    result.log = relative.as_posix()
    atomic_text(root / relative, result.stdout)
    atomic_text(root / relative.with_name(f"{result.target_id}.stderr.log"), result.stderr)
    atomic_text(root / ".state" / "facts" / result.stage / f"{result.target_id}.tsv",
                table_text(FACT_FIELDS, [asdict(result)]))


def write_receipt(root: Path, target_id: str, data: bytes | None, detail: str = "") -> None:
    """发布目标累计日志及哈希回执；data 为 None 表示未能取回。"""
    receipt = dict(status="FAIL", detail=detail or "日志未回传", sha256="")
    if data is not None:
        path = root / "logs" / "groups" / (target_id + ".log")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        receipt.update(status="PASS", detail="日志已回收", sha256=hashlib.sha256(data).hexdigest())
    atomic_text(root / ".state" / "log-collection" / (target_id + ".json"),
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")


def publish_stages(root: Path, target_id: str, stages: list[str]) -> None:
    """单节点操作和容器管理没有组日志，由已落盘的阶段输出汇总。"""
    chunks = []
    for stage in stages:
        fact = read_table(root / ".state/facts" / stage / (target_id + ".tsv"))[0]
        if fact["status"] == "BLOCKED":
            continue
        chunks.append(f"[TEST-BEGIN] test={stage} token={fact['token']}\n".encode())
        for stream in ("stdout", "stderr"):
            chunks.append((root / "logs" / stage / f"{target_id}.{stream}.log").read_bytes() + b"\n")
        chunks.append(f"[TEST-END] test={stage} token={fact['token']} exit_code={fact['transport_exit_code']}\n".encode())
    write_receipt(root, target_id, b"".join(chunks))


def remote_dir(request: Request, run_id: str) -> str:
    """每个节点的本次任务目录；要求为节点本地路径，结果文件不按节点区分。"""
    return f"{request.remote_workdir.rstrip('/')}/{run_id}"


def _config(request: Request, run_id: str) -> str:
    """生成目标端唯一配置，全部值经 Shell 引用，payload 只 source 不解析。"""
    directory = remote_dir(request, run_id)
    values = dict(HCU_RUN_ID=run_id, HCU_RUN_DIR=directory, HCU_OPERATION=request.operation,
                  HCU_SCENARIO=request.scenario, HCU_CONTAINER=request.container, HCU_IMAGE=request.image,
                  HCU_CONTAINER_USER=request.container_user, HCU_ENV_SCRIPT=request.env_script,
                  HCU_WORKDIR=request.container_workdir or directory, HCU_PYTHON=request.container_python,
                  HCU_GPUS_PER_NODE=request.gpus_per_node, HCU_TESTS=" ".join(request.tests),
                  HCU_TEST_TIMEOUT=request.timeout, HCU_CHECK_TIMEOUT=min(request.timeout, CHECK_TIMEOUT),
                  HCU_PEER_PORT=peer_port(request), HCU_SKIP_IDLE=int(request.skip_idle_check),
                  HCU_RELEASE=int(request.release), HCU_DEPENDENCY_MANIFEST=request.dependency_manifest,
                  HCU_PORT=request.port, HCU_SSH_DIR=request.ssh_dir, HCU_IMAGE_TAR=request.image_tar,
                  HCU_REMOTE_WORKDIR=request.remote_workdir, HCU_YES=int(request.yes))
    if request.operation == "deepep":
        from .operations.deepep import unsupported_reason
        errors = {mode: unsupported_reason(request, mode) for mode in request.tests}
        values["HCU_DEEPEP_RUNNABLE_TESTS"] = " ".join(mode for mode, error in errors.items() if not error)
        values.update({f"HCU_DEEPEP_UNSUPPORTED_{mode.upper()}": error for mode, error in errors.items()})
    script = "".join(f"{name}={shlex.quote(str(value))}\n" for name, value in values.items())
    for name, values in (("HCU_VOLUMES", request.volumes), ("HCU_DOCKER_ARGS", request.docker_args)):
        script += name + "=(" + " ".join(shlex.quote(value) for value in values) + ")\n"
    return script


def _bundle(root: Path, request: Request, run_id: str) -> Path:
    """把 payload、计划和测试源码打成一个 tar，千节点只传一个文件。"""
    files = {"RUN_ID": (run_id + "\n").encode(), "config.env": _config(request, run_id).encode()}
    payloads = Path(request.root) / "hcu_envcheck/payloads"
    for path in sorted(payloads.iterdir()):
        if path.is_file():
            files["payloads/" + path.name] = path.read_bytes().replace(b"\r\n", b"\n")
    for path in sorted((root / "plan").rglob("*")):
        if path.is_file() and path.name not in ("request.json", "request.tsv"):
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    if request.operation in ("deepep", "byte-flux"):
        library = "DeepEP" if request.operation == "deepep" else "flux"
        override = request.deepep_source if library == "DeepEP" else request.flux_source
        snapshot, meta = source_snapshot(Path(request.root), library, override)
        files.update({f"source/{library}/{name}": data for name, data in snapshot.items()})
        files[f"source/{library}/SOURCE_METADATA.json"] = (json.dumps(meta) + "\n").encode()
        atomic_text(root / "plan" / "source.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    path = root / "bundle.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    return path


def _hosts(path: Path, nodes) -> Path:
    """写一份 clush --hostfile 节点清单。"""
    atomic_text(path, "".join(node + "\n" for node in nodes))
    return path


def _gather(transport, root: Path, hosts: Path, remote: str, label: str) -> Path:
    """回收各节点结果包并解压到 collected/<label>/<node>；未取回的节点重试一次。"""
    directory = root / "collected" / label
    directory.mkdir(parents=True, exist_ok=True)
    log = root / "clush" / (label + "-gather.log")
    nodes = hosts.read_text(encoding="utf-8").split()
    transport.rcopy(hosts, remote + "/out.tgz", directory, COPY_TIMEOUT, log)
    missing = [node for node in nodes if not (directory / f"out.tgz.{node}").is_file()]
    if missing:
        retry = _hosts(root / "clush" / (label + "-retry.hosts"), missing)
        transport.rcopy(retry, remote + "/out.tgz", directory, COPY_TIMEOUT, log)
    for node in nodes:
        package = directory / f"out.tgz.{node}"
        if package.is_file():
            try:
                with tarfile.open(package) as archive:
                    archive.extractall(directory / node, filter="data")
            except (tarfile.TarError, OSError) as error:
                (directory / f"{node}.error").write_text(str(error), encoding="utf-8")
    return directory


def _node_result(directory: Path, node: str) -> NodeResult:
    """读取一个节点的状态和日志；缺失时返回明确原因，不当作通过。"""
    results = directory / node / "results"
    path = results / "node.status"
    if not path.is_file():
        return NodeResult(node, results, "FAIL", "NODE_NO_RESULT", 1, "节点无结果回传：不可达、分发失败或执行中断")
    cells = path.read_text(encoding="utf-8", errors="replace").rstrip("\n").split("\t")
    if len(cells) != 4 or not cells[2].isdigit():
        return NodeResult(node, results, "FAIL", "NODE_STATUS_INVALID", 1, "节点状态文件格式错误")
    return NodeResult(node, results, cells[0], cells[1], int(cells[2]), cells[3])


def _run_batches(request, transport, root, run_id, entry, mode, limit, ready=None):
    """按计划顺序执行；每批回收结束后才启动下一批，预检失败不重新分组。"""
    batches = sorted((root / "plan").glob("clushhostfile.*"), key=lambda path: int(path.suffix[1:]))
    if not batches:
        raise ValueError("计划缺少 clushhostfile 批次清单")
    directory, executed = root / "collected" / mode, set()
    for index, batch in enumerate(batches, 1):
        if transport.cancelled.is_set():
            break
        nodes = batch.read_text(encoding="utf-8").splitlines()
        selected = [node for node in nodes if ready is None or node in ready]
        print(f"[BATCH {index}/{len(batches)}] {mode} · {len(selected)} 个组首节点", flush=True)
        if not selected:
            continue
        hosts = batch if selected == nodes else _hosts(root / ".state" / "batches" / batch.name, selected)
        transport.fanout(hosts, entry + " " + mode, limit, root / "clush" / f"{mode}-{index:04d}.log")
        executed.update(selected)
        directory = _gather(transport, root, hosts, remote_dir(request, run_id), mode)
    return directory, executed


def _single(request, plan, operation, transport, root, run_id, entry, started):
    """情况1：每节点一组，容器阶段在节点内部推进，最后发布全部阶段事实。"""
    container = request.operation.startswith("container-")
    mode = "container" if container else "node"
    limit = len(plan.stages) * (request.timeout + 5) + 120 if container else request.timeout + 120
    directory, executed = _run_batches(request, transport, root, run_id, entry, mode, limit)
    for target in plan.targets:
        node = _node_result(directory, target.leader)
        for stage in plan.stages:
            if target.leader not in executed and transport.cancelled.is_set():
                result = TaskResult(target.id, target.leader, stage, "CANCELLED", "CANCELLED", 130,
                                    detail="任务已取消，本批次未启动", transport_exit_code=130)
            else:
                result = operation.interpret(node, target, stage, root, run_id)
            result.token = f"{run_id}_{target.id}_{stage}"
            result.started_epoch = result.started_epoch or started
            result.ended_epoch = result.ended_epoch or int(time.time())
            save_fact(root, result)
        publish_stages(root, target.id, plan.stages)


def _member_text(member: NodeResult) -> str:
    """组员预检日志原文，保留给 DeepEP 环境明细解析。"""
    path = member.results / "node.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _image(member: NodeResult) -> str:
    """读取容器实际镜像 ID，供组内一致性比较。"""
    path = member.results / "image.id"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def _prepare_verdict(request, members: list[NodeResult]) -> tuple[int, str, str]:
    """返回组预检结论；hostfile 顺序中的首个失败节点为主因。"""
    primary = next((member for member in members if member.status != "PASS"), None)
    if primary:
        return primary.exit_code or 1, primary.reason, f"首个失败节点 {primary.node}: {primary.detail}"
    if is_container(request) and len({_image(member) for member in members}) != 1:
        return 69, "IMAGE_ID_MISMATCH", "组员镜像 ID 不一致"
    return 0, "PREPARE_READY", "全部组员预检通过"


def _write_prepared(root: Path, target: Target, members: list[NodeResult], code: int) -> None:
    """保存逐节点预检回执，报告据此输出 prepare-nodes.tsv。"""
    rows = [dict(node=item.node, prepare_result=dict(
        node=item.node, phase="prepare", status=item.status, reason_code=item.reason, exit_code=item.exit_code,
        detail=item.detail, command_exit_code=item.exit_code)) for item in members]
    atomic_text(root / ".state/prepared" / f"{target.id}.json",
                json.dumps(dict(ready=not code, members=rows), ensure_ascii=False, indent=2) + "\n")


def _group_failure(directory: Path, node: str) -> tuple[int, str, str] | None:
    """读取组首节点的组级失败；文件不存在表示组内连通性通过。"""
    path = directory / node / "results" / "group.status"
    if not path.is_file():
        return None
    cells = path.read_text(encoding="utf-8", errors="replace").rstrip("\n").split("\t")
    if len(cells) != 4 or not cells[2].isdigit():
        return 1, "GROUP_STATUS_INVALID", "组状态文件格式错误"
    return int(cells[2]), cells[1], cells[3]


def _test_fact(request, target, stage, token, section, group_error, skip, started) -> TaskResult:
    """把组日志的一段转成执行事实；未执行的项保留跳过原因。"""
    name = request.operation.upper().replace("-", "_")
    result = TaskResult(target.id, target.leader, stage, token=token, started_epoch=started, ended_epoch=int(time.time()))
    if section is None:
        result.status, result.reason_code = ("BLOCKED", "GROUP_SKIPPED") if skip else ("FAIL", "TEST_NOT_EXECUTED")
        result.exit_code = result.transport_exit_code = 1
        result.detail = skip or group_error or "组日志中没有本测试项"
        return result
    code = section["exit_code"]
    if code is None:
        result.status, result.reason_code, result.detail = "FAIL", "TEST_INCOMPLETE", section["error"]
        result.exit_code = result.transport_exit_code = 1
    else:
        result.exit_code = result.transport_exit_code = code
        result.status = "PASS" if not code else "FAIL"
        result.reason_code = (f"{name}_EXECUTED" if not code else "TASK_TIMEOUT" if code == 124
                              else f"{name}_EXECUTION_FAILED")
        result.detail = "执行完成，结论由最终报告解析" if not code else "执行失败，详见组日志"
    result.stdout = section["text"]
    return result


def _active(request, plan, transport, root, run_id, entry, started):
    """情况2：全部节点预检，全员通过的组由首节点执行，最后逐组发布事实。"""
    hosts, remote = root / "plan" / "all.hosts", remote_dir(request, run_id)
    transport.fanout(hosts, entry + " check", min(request.timeout, CHECK_TIMEOUT) + 120, root / "clush" / "check.log")
    checked = _gather(transport, root, hosts, remote, "check")
    verdicts, ready = {}, []
    for target in plan.targets:
        members = [_node_result(checked, node) for node in target.nodes]
        verdicts[target.id] = (members, _prepare_verdict(request, members))
        _write_prepared(root, target, members, verdicts[target.id][1][0])
        if not verdicts[target.id][1][0]:
            ready.append(target)
    print(f"[PREPARE] {len(ready)}/{len(plan.targets)} 个组通过预检", flush=True)
    collected, executed = _run_batches(request, transport, root, run_id, entry, "group",
                                      request.timeout * len(request.tests) + CHECK_TIMEOUT,
                                      {target.leader for target in ready})
    for target in plan.targets:
        members, (code, reason, detail) = verdicts[target.id]
        sections, group_error, skip = {}, "", ""
        if code:
            skip = detail
            write_receipt(root, target.id, b"")
        elif target.leader not in executed:
            skip = "任务已取消，本组未启动" if transport.cancelled.is_set() else "本组未启动"
            write_receipt(root, target.id, b"")
        else:
            path = collected / target.leader / "results" / (target.id + ".log")
            write_receipt(root, target.id, path.read_bytes() if path.is_file() else None, "组首节点日志未回传")
            sections, group_error = read_group(root, target.id)
            failed = _group_failure(collected, target.leader)
            if failed:
                code, reason, detail = failed
                skip = detail
        save_fact(root, TaskResult(target.id, target.leader, "prepare", "FAIL" if code else "PASS", reason, code,
                                   image_id=_image(members[0]), detail=detail, started_epoch=started,
                                   ended_epoch=int(time.time()), token=f"{run_id}_{target.id}_prepare",
                                   transport_exit_code=code, stdout="".join(map(_member_text, members))))
        previous_failure = ""
        for stage in plan.stages[1:]:
            token = f"{run_id}_{target.id}_{stage}"
            section = sections.get((stage, token))
            result = _test_fact(request, target, stage, token, section, group_error, skip or previous_failure, started)
            if not code and target.leader not in executed and transport.cancelled.is_set():
                result.status, result.reason_code, result.exit_code, result.transport_exit_code = "CANCELLED", "CANCELLED", 130, 130
            save_fact(root, result)
            if request.operation == "rccl" and section and not (group_error or section["error"]) and section["exit_code"]:
                previous_failure = f"本组 {stage} 执行失败（退出码 {section['exit_code']}），本项未启动"


def run(request: Request, plan: Plan, operation, transport, root: Path, run_id: str) -> None:
    """分发后按操作形态执行；事实全部落盘才返回。"""
    remote = remote_dir(request, run_id)
    quoted = shlex.quote(remote)
    started = int(time.time())
    hosts = root / "plan" / "all.hosts"
    bundle = _bundle(root, request, run_id)
    log = root / "clush" / "dist.log"
    print(f"[DIST] {sum(len(target.nodes) for target in plan.targets)} 个节点", flush=True)
    transport.fanout(hosts, "mkdir -p -- " + quoted, COPY_TIMEOUT, log)
    transport.copy(hosts, bundle, remote, COPY_TIMEOUT, log)
    transport.fanout(hosts, f"tar -xf {quoted}/bundle.tar -C {quoted} && mkdir -p {quoted}/results "
                            f"&& chmod 1777 {quoted}/results", COPY_TIMEOUT, log)
    entry = "bash " + shlex.quote(remote + "/payloads/entry.sh")
    if request.operation in ACTIVE_OPERATIONS:
        _active(request, plan, transport, root, run_id, entry, started)
    else:
        _single(request, plan, operation, transport, root, run_id, entry, started)
