"""离线脚本测试命令替身：只记录调用、读写测试目录和输出预设数据。

由 shell_support 在临时目录生成带当前 Python shebang 的副本。
不执行收到的 Docker/SSH/MPI/目标 Python 命令。
"""
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["HCU_STUB_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
with Path(os.environ["HCU_STUB_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(dict(command=name, argv=args)) + "\n")


def container():
    return state.setdefault("container", dict(id="a" * 64, name="unit-container", exists=True,
                            running=True, image="sha256:" + "b" * 64, started="start-1", pids=[600001]))


def main():
    if name == "hostname":
        return 0, "192.0.2.1\n" if args == ["-I"] else state.get("hostname", "node001") + "\n", ""
    if name == "sleep":
        return 0, "", ""
    if name == "ss":
        return 0, "LISTEN test\n" if state.get("port_busy") else "", ""
    if name == "ssh":
        return state.get("ssh_exit", 0), state.get("ssh_output", "hcu_unit_run") + "\n", state.get("ssh_stderr", "")
    if name == "mpirun":
        command = " ".join(args)
        code = state.get("mpi_exit", 0)
        for test, value in state.get("mpi_exit_by_test", {}).items():
            if test + "_perf" in command:
                code = value
        return code, state.get("mpi_output", "unit native output\n"), ""
    if name.endswith("_perf"):
        return 0, "unit native output\n", ""
    if name == "target-python":
        command = " ".join(args)
        if "dependency_check.py" in command:
            return state.get("dependency_exit", 0), "__HCU_DEPENDENCY__\t{}\n", ""
        if "gpu_check.py" in command:
            return state.get("gpu_exit", 0), "[GPU-CHECK] unit output\n", ""
        if "platform_probe.py" in command:
            return state.get("python_exit", 0), json.dumps(state.get("platform_payload", {})) + "\n", ""
        return state.get("python_exit", 0), "unit target output\n", ""
    if name == "hy-smi":
        if "--showpid" in args or "--showpids" in args:
            pids = state.get("busy_pids", [600001])
            return 0, "KFD PID\n" + "".join(f"{pid} unit-worker\n" for pid in pids), ""
        sample = state.get("smi_sample_count", 0)
        state["smi_sample_count"] = sample + 1
        values = state.get("vram_samples", [0])
        vram = values[min(sample, len(values) - 1)]
        text = "Card Temp Power Clock Fan VRAM% HCU%\n"
        text += "".join(f"{card} 30 20 100 0 {vram}% 0%\n" for card in range(state.get("cards", 2)))
        return state.get("smi_exit", 0), text, ""
    if name == "cat":
        output = []
        for argument in args:
            if argument.startswith("/proc/") and argument.endswith("/stat"):
                pid = argument.split("/")[2]
                reads = state.setdefault("pid_reads", {})
                index = reads.get(pid, 0)
                reads[pid] = index + 1
                starts = state.get("pid_starts", {}).get(pid, [123])
                start = starts[min(index, len(starts) - 1)]
                output.append(f"{pid} (unit worker) " + " ".join(["S"] + ["0"] * 18 + [str(start)]) + "\n")
            else:
                path = Path(argument).resolve()
                if not path.is_relative_to(Path(os.environ["HCU_STUB_ROOT"]).resolve()):
                    return 125, "", "unexpected cat outside test directory"
                output.append(path.read_text(encoding="utf-8"))
        return 0, "".join(output), ""
    if name == "docker":
        item = container()
        if not args:
            return 125, "", "docker stub needs an action"
        action = args[0]
        if action == "info":
            return state.get("docker_info_exit", 0), "", state.get("docker_info_error", "")
        if action == "image":
            return 0, state.get("expected_image", "sha256:" + "b" * 64) + "\n", ""
        if action in ("pull", "load", "cp"):
            return 0, "", ""
        if action == "ps":
            ids = [item["id"]] + state.get("extra_container_ids", [])
            return 0, "\n".join(ids) + "\n", ""
        if action == "top":
            # 非占卡容器仍有普通进程，不能用空表代表其运行状态。
            pids = item.get("pids", []) if args[1] == item["id"] else [600099]
            return 0, "PID\n" + "".join(f"{pid}\n" for pid in pids), ""
        if action == "inspect":
            if state.get("inspect_exit"):
                return state["inspect_exit"], "", "docker query failed"
            queried = args[-1]
            if queried in state.get("extra_container_ids", []):
                item = dict(item, id=queried, name="unoccupied-container", pids=[600099])
            if not item["exists"] or queried not in (item["name"], item["id"]):
                return 1, "", "No such object: " + queried
            fmt = args[args.index("-f") + 1]
            running = "true" if item["running"] else "false"
            if fmt == "{{.State.Running}}":
                if state.get("inspect_after_stop_exit") and not item["running"]:
                    return state["inspect_after_stop_exit"], "", "query after stop failed"
                return 0, running + "\n", ""
            if fmt == "{{.State.StartedAt}}|{{.State.Running}}":
                return 0, item["started"] + "|" + running + "\n", ""
            if fmt == "{{.Name}}|{{.State.StartedAt}}|{{.State.Running}}":
                return 0, "/" + item["name"] + "|" + item["started"] + "|" + running + "\n", ""
            if fmt == "{{.Id}}|{{.State.Running}}|{{.Image}}":
                return 0, item["id"] + "|" + running + "|" + item["image"] + "\n", ""
            if fmt == "{{.State.Running}}|{{.Image}}":
                return 0, running + "|" + item["image"] + "\n", ""
            if fmt == "{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}":
                return 0, "host|true\n", ""
            if ".Mounts" in fmt:
                return 0, "\n".join(state.get("mount_destinations", [])), ""
            return 125, "", "unexpected docker inspect format: " + fmt
        if action == "run":
            if "--rm" in args:
                return state.get("image_tools_exit", 0), "", ""
            item.update(exists=True, running=True, id="c" * 64,
                        image=state.get("expected_image", "sha256:" + "b" * 64))
            return state.get("create_exit", 0), item["id"] + "\n", ""
        if action == "stop":
            code = state.get("stop_exit", 0)
            if not code:
                item["running"] = False
            return code, item["id"] + "\n", ""
        if action == "rm":
            code = state.get("remove_exit", 0)
            if not code:
                item["exists"] = False
            return code, "", ""
        if action == "exec":
            index = next((i for i, value in enumerate(args) if value == item["id"]), None)
            if index is None:
                return 125, "", "unexpected docker exec identity"
            command = args[index + 1:]
            joined = " ".join(command)
            if "getent passwd" in joined:
                return 0, "unituser\t1000\t1000\t/home/unituser\n", ""
            if command[:1] == ["readlink"]:
                return 0, "/home/unituser/.ssh\n", ""
            if command[:1] in (["mkdir"], ["test"]):
                return 0, "", ""
            if command[:2] == ["bash", "-s"]:
                # 保存脚本参数供回环身份查询使用，不执行 stdin 的 sshd 初始化脚本。
                tail = command[command.index("--") + 1:]
                state["ssh_identity"] = tail[1] + " " + tail[2]
                sys.stdin.read()
                return 0, "", ""
            if command[:1] == ["ssh"]:
                return 0, state.get("ssh_identity_output", state.get("ssh_identity", "missing")) + "\n", ""
            return 125, "", "unexpected docker exec command: " + joined
    return 125, "", "unexpected stub command: " + name


try:
    code, output, error = main()
finally:
    state_path.write_text(json.dumps(state), encoding="utf-8")
sys.stdout.write(output)
sys.stderr.write(error)
raise SystemExit(code)
