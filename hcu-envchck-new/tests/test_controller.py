"""跨模块离线流程：真实 CLI → 计划 → 控制器 → 调度 → 回执 → 最终报告。"""
import contextlib
import io
import json
import signal
from unittest.mock import patch

from hcu_envcheck import cli, controller, dispatch
from hcu_envcheck.planning import build_plan
from hcu_envcheck.registry import get_operation
from tests.helpers import (CID, FakeTransport, IMAGE_ID, RUN_ID, Workspace, fixture,
                           idle_sample, section)
from tests.test_rccl import baseline_text


class ControllerTests(Workspace):
    def packets(self, request, plan):
        packages = {}
        for target in plan.targets:
            node = target.leader
            if request.operation.startswith("container-"):
                files = {"node.status": "PASS\tOK\t0\tdone\n"}
                for stage in plan.stages:
                    files[stage + ".status"] = "PASS\tOK\t0\tdone\n"
                    files[stage + ".meta"] = "\n".join([RUN_ID, target.id, stage,
                        f"{RUN_ID}_{target.id}_{stage}", CID, IMAGE_ID, "1", "2"]) + "\n"
                    files[stage + ".log"], files[stage + ".stderr.log"] = "stage done", ""
                packages[("container", node)] = files
            elif request.operation in ("platform", "idle-check", "checkdeepep"):
                reason = "CHECK_OK" if request.operation == "checkdeepep" else "OK"
                files = {"node.status": f"PASS\t{reason}\t0\tdone\n", "container.capability": "host|true\n"}
                if request.operation == "platform":
                    token = f"{RUN_ID}_{target.id}_platform"
                    payload = json.dumps(json.loads(fixture("platform.json")))
                    files["node.log"] = f"[PLATFORM-BEGIN] token={token}\n{payload}\n[PLATFORM-END] token={token} exit_code=0\n"
                elif request.operation == "idle-check":
                    files["idle.log"] = idle_sample(node=node)
                    if request.release:
                        files["idle.log"] = (idle_sample(vram=70, node=node) +
                            f"RELEASE\t{CID}\tunit-container\tCONFIRMED\n" + files["idle.log"])
                else:
                    files["node.log"] = fixture("checkdeepep.log")
                packages[("node", node)] = files
            else:
                for member in target.nodes:
                    packages[("check", member)] = {"node.status": "PASS\tPREPARE_READY\t0\tready\n",
                        "image.id": IMAGE_ID, "node.log": f"__HCU_DEEPEP_ENV__\t{member}\t0\n"}
                body = ""
                for stage in request.tests:
                    if request.operation == "rccl":
                        text = fixture("rccl-valid.log").replace("node001", node)
                    elif request.operation == "deepep":
                        text = (f"[DEEPEP-LAUNCH] node={node} nnodes=1 node_rank=0 gpu_workers=2 mode=intranode\n"
                                "[tuning] Best dispatch result 10.0 GB/s (NVL)\n")
                    else:
                        text = "[BYTE-FLUX-LAUNCH] " + json.dumps(dict(node=node, workers=2)) + "\n"
                        text += "".join(f"torch #{rank}: total 2.0 ms\nflux #{rank}: total 1.0 ms\n" for rank in range(2))
                    body += section(stage, text, target=target.id)
                packages[("group", node)] = {target.id + ".log": body}
        return packages

    def run_cli(self, operation, scenario="per-node-container", release=False):
        # 与真实 CLI 构造同样的请求，仅用于准备外部节点回执。
        request = self.request(operation, scenario=scenario, gpus_per_node=2,
                               tests=["all_reduce"] if operation == "rccl" else [], release=release,
                               yes=operation in ("container-delete", "container-recreate"))
        selected = get_operation(operation)
        selected.validate(request)
        plan = build_plan(request, selected)
        transport = FakeTransport(self.packets(request, plan))
        output = self.work / operation / scenario / ("release" if release else "check")
        args = [scenario, operation, "-f", str(self.hostfile), "-o", str(output)]
        if operation != "idle-check" and scenario == "per-node-container":
            args += ["--container", "unit-container", "-i", "unit-image:1"]
        if scenario != "per-node-container":
            args += ["--env-script", "/test/env.sh"]
        if operation in ("rccl", "deepep", "byte-flux"):
            args += ["--gpus-per-node", "2"]
        if operation == "rccl":
            baseline = self.work / "baseline.conf"
            baseline.write_text(baseline_text(), encoding="utf-8")
            args += ["--tests", "all_reduce", "--baseline-file", str(baseline), "--baseline-profile", "unit"]
        if operation in ("container-delete", "container-recreate"):
            args += ["--yes"]
        if release:
            args += ["--release"]
        def snapshot(root, library, override):
            name = "tests/test_intranode.py" if library == "DeepEP" else "test/python/test_ag_kernel.py"
            return {name: b"# unit source\n"}, {"library": library, "commit": "a" * 40, "files": {}}
        with patch("hcu_envcheck.controller.Transport", return_value=transport), \
                patch("hcu_envcheck.controller.datetime") as date, \
                patch("hcu_envcheck.controller.secrets.token_hex", return_value="_run"), \
                patch("hcu_envcheck.dispatch.source_snapshot", side_effect=snapshot), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            date.now.return_value.strftime.return_value = "_unit"
            code = cli.main(args)
        roots = list(output.glob("*"))
        self.assertEqual(len(roots), 1)
        root = roots[0]
        report = json.loads((root / "results.json").read_text(encoding="utf-8"))
        self.assertTrue((root / ".state/execution.closed").is_file())
        self.assertTrue((root / "summary.md").is_file())
        self.assertEqual(json.loads((root / "run.json").read_text(encoding="utf-8"))["state"], "FINISHED")
        self.assertFalse(any(call[0] == "fanout" and call[2].endswith(" cleanup") for call in transport.calls))
        return code, report, root

    def test_full_offline_chain_all_operations(self):
        for operation in ("container-create", "container-status", "container-recreate", "container-delete",
                          "platform", "idle-check", "checkdeepep", "rccl", "deepep", "byte-flux"):
            with self.subTest(operation=operation):
                code, report, root = self.run_cli(operation)
                if operation == "platform":
                    self.assertIn(code, (0, 2))
                    self.assertTrue((root / "platform-overview.json").is_file())
                else:
                    self.assertEqual(code, 0, report)
                    self.assertEqual(report["status"], "PASS")

    def test_conda_rccl_offline_chain_both_scenarios(self):
        for scenario in ("shared-conda", "node-local-conda"):
            with self.subTest(scenario=scenario):
                code, report, _ = self.run_cli("rccl", scenario)
                self.assertEqual(code, 0, report)

    def test_release_offline_chain_generates_report(self):
        code, report, root = self.run_cli("idle-check", release=True)
        self.assertEqual(code, 0, report)
        self.assertTrue(report["idle_check"]["release_requested"])
        self.assertTrue((root / "idle-results.tsv").is_file())

    def test_report_exception_preserves_evidence_and_skips_remote_cleanup(self):
        request = self.request("container-status")
        plan = build_plan(request, get_operation(request.operation))
        transport = FakeTransport(self.packets(request, plan))
        with patch("hcu_envcheck.controller.Transport", return_value=transport), \
                patch("hcu_envcheck.reporting.engine.generate", side_effect=ValueError("report failure")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = controller.run(request, plan, get_operation(request.operation))
        root = next((self.work / "output").iterdir())
        self.assertEqual(code, 70)
        self.assertTrue((root / "report-error.log").is_file())
        self.assertTrue((root / "collected/container").is_dir())
        self.assertFalse(any(call[0] == "fanout" and call[2].endswith(" cleanup") for call in transport.calls))

    def test_cancelled_run_returns_130_and_records_unstarted_targets(self):
        request = self.request("container-status")
        plan = build_plan(request, get_operation(request.operation))
        transport = FakeTransport()
        handlers = {}
        def register(signum, handler):
            previous = handlers.get(signum, signal.SIG_DFL)
            handlers[signum] = handler
            return previous
        original_dispatch = dispatch.run
        def cancel_before_dispatch(*args, **kwargs):
            # 调用控制器登记的回调，同时设置控制器与传输层状态，不发送真实信号。
            handlers[signal.SIGINT](signal.SIGINT, None)
            return original_dispatch(*args, **kwargs)
        with patch("hcu_envcheck.controller.Transport", return_value=transport), \
                patch("hcu_envcheck.controller.signal.signal", side_effect=register), \
                patch("hcu_envcheck.controller.dispatch.run", side_effect=cancel_before_dispatch), \
                contextlib.redirect_stdout(io.StringIO()):
            code = controller.run(request, plan, get_operation(request.operation))
        root = next((self.work / "output").iterdir())
        report = json.loads((root / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 130)
        self.assertEqual(json.loads((root / "run.json").read_text(encoding="utf-8"))["state"], "CANCELLED")
        self.assertTrue(all(row["status"] == "CANCELLED" for row in report["results"]))
