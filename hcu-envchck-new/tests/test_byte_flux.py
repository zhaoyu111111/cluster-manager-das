"""byte-flux 使用参数确定 TP；验证启动证据、rank 覆盖及有限正耗时。"""
import json

from hcu_envcheck.dispatch import write_receipt
from hcu_envcheck.operations import byte_flux
from hcu_envcheck.reporting.byte_flux_report import parse_node
from tests.helpers import Workspace, fact_row, section


class ByteFluxTests(Workspace):
    def test_tp_follows_gpus_per_node_without_fixed_eight(self):
        for gpus in (1, 2, 4, 8, 16):
            with self.subTest(gpus=gpus):
                request = self.request("byte-flux", gpus_per_node=gpus)
                byte_flux.validate(request)
                self.assertEqual(byte_flux.stages(request), ["prepare", f"tp{gpus}"])

    def test_group_size_custom_tests_and_foreign_options_rejected(self):
        for changes in (dict(group_size=2), dict(tests=["ag"]), dict(provided={"baseline_profile"})):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                byte_flux.validate(self.request("byte-flux", **changes))

    def body(self, gpus=4):
        launch = dict(node="node001", workers=gpus, devices=",".join(map(str, range(gpus))))
        return "[BYTE-FLUX-LAUNCH] " + json.dumps(launch) + "\n" + "".join(
            f"torch #{rank}: total 2.0 ms\nflux #{rank}: total 1.0 ms\n" for rank in range(gpus))

    def parse(self, body, gpus=4, code=0):
        write_receipt(self.root, "group-001", section(f"tp{gpus}", body, code).encode())
        return parse_node((self.root, fact_row(stage=f"tp{gpus}", code=code,
                          reason_code="BYTE_FLUX_EXECUTION_FAILED" if code else "BYTE_FLUX_EXECUTED"), gpus))

    def test_complete_output_and_metrics_per_rank(self):
        for gpus in (1, 2, 4, 8):
            with self.subTest(gpus=gpus):
                result, metrics = self.parse(self.body(gpus), gpus)
                self.assertEqual(result["status"], "PASS")
                self.assertEqual({int(row["rank"]) for row in metrics}, set(range(gpus)))

    def test_missing_rank_launch_mismatch_and_zero_time(self):
        for body, expected in ((self.body().replace("flux #3: total 1.0 ms\n", ""), "METRIC_COVERAGE_INCOMPLETE"),
                               (self.body().replace('"workers": 4', '"workers": 8'), "LAUNCH_EVIDENCE_INVALID"),
                               (self.body().replace("flux #0: total 1.0 ms", "flux #0: total 0.0 ms"), "METRIC_INVALID"),
                               (self.body().replace("node001", "other-node"), "LAUNCH_EVIDENCE_INVALID")):
            with self.subTest(expected=expected):
                self.assertEqual(self.parse(body)[0]["reason_code"], expected)

    def test_native_command_failure_is_preserved(self):
        result, _ = self.parse("AssertionError: comparison failed", code=1)
        self.assertEqual(result["reason_code"], "BYTE_FLUX_EXECUTION_FAILED")
        self.assertEqual(result["exit_code"], "1")

    def test_prepare_does_not_require_metrics(self):
        result, metrics = parse_node((self.root, fact_row(stage="prepare"), 4))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(metrics, [])
