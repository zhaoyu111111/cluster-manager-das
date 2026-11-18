"""RCCL 完整性、正确性、规模和性能分别判定；不运行 RCCL。"""
from hcu_envcheck.operations import rccl
from hcu_envcheck.rccl_baseline import BaselineConfigError, COLLECTIVES, load_baseline
from hcu_envcheck.reporting import rccl_report
from hcu_envcheck.reporting.rccl_output import build_result, compare_result, parse_log, validate_output
from hcu_envcheck.dispatch import write_receipt
from tests.helpers import Workspace, fact_row, fixture, section


def baseline_text(nodes=1, gpus=2):
    return (f"[defaults]\nformat_version=2\nerror_pct=10\n[unit]\nkind=other\ngpu_model=TEST_CARD\n"
            f"gpus_per_node={gpus}\nnode_count={nodes}\n" + "".join(f"{name}=20\n" for name in COLLECTIVES))


class RcclTests(Workspace):
    def evidence(self, text=None):
        return parse_log((fixture("rccl-valid.log") if text is None else text).splitlines())

    def test_default_ten_collectives_and_requested_order(self):
        request = self.request()
        rccl.validate(request)
        self.assertEqual(request.tests, list(COLLECTIVES))
        request.tests = ["broadcast", "all_reduce"]
        self.assertEqual(rccl.stages(request), ["prepare", "broadcast", "all_reduce"])
        for tests in (["ag"], ["all_reduce", "all_reduce"]):
            with self.subTest(tests=tests), self.assertRaises(ValueError):
                rccl.validate(self.request(tests=tests))

    def test_four_metric_columns_remain_independent(self):
        evidence = self.evidence()
        self.assertIsNone(validate_output(evidence, 2))
        keys = ("out_of_place_algbw_gbps", "out_of_place_busbw_gbps", "in_place_algbw_gbps", "in_place_busbw_gbps")
        self.assertEqual([evidence.metrics[key] for key in keys], [30, 24, 32, 27])
        self.assertEqual(evidence.metrics["actual_np"], 2)

    def test_missing_middle_row_rejected(self):
        text = "\n".join(line for line in fixture("rccl-valid.log").splitlines() if not line.startswith("8 2 "))
        self.assertEqual(validate_output(self.evidence(text), 2)[0], "DATA_TABLE_INCOMPLETE")

    def test_invalid_numbers_wrong_counts_missing_end_and_scale(self):
        original = fixture("rccl-valid.log")
        cases = [(original.replace("1.0 20 16 0", "1.0 nan 16 0"), 2, "DATA_ROWS_INVALID"),
                 (original.replace("1.0 20 16 0", "1.0 20 16 1"), 2, "CORRECTNESS_FAILED"),
                 (original.replace("rccl test done", ""), 2, "END_EVIDENCE_MISSING"),
                 (original, 4, "SCALE_MISMATCH"),
                 (original + "\n# Rank 0 Group 0 Pid 100 on node001 device 0\n", 2, "ACTUAL_SCALE_DUPLICATE"),
                 ("# Avg bus bandwidth : 999\n", 2, "DATA_TABLE_MISSING")]
        for text, np, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(validate_output(self.evidence(text), np)[0], expected)

    def test_root_collective_na_validation_column_allowed(self):
        text = fixture("rccl-valid.log").replace("27 0", "27 N/A")
        self.assertIsNone(validate_output(self.evidence(text), 2))

    def test_command_failure_not_replaced_by_performance_failure(self):
        result = build_result(self.evidence(), 2, fact_row(code=7))
        compare_result(result, {"baseline_busbw_gbps": 1000, "margin_pct": 0})
        self.assertEqual(result["reason_code"], "RCCL_EXECUTION_FAILED")
        self.assertEqual(result["exit_code"], "7")

    def test_performance_threshold_exact_boundary_and_missing_baseline(self):
        reference = {"baseline_busbw_gbps": 30, "margin_pct": 10}
        result = build_result(self.evidence(), 2, fact_row())
        compare_result(result, reference)
        self.assertEqual(result["status"], "PASS")  # 27 == 30 * 0.9
        result = build_result(self.evidence(), 2, fact_row())
        compare_result(result, {"baseline_busbw_gbps": 31, "margin_pct": 10})
        self.assertEqual(result["reason_code"], "BELOW_BASELINE")
        result = build_result(self.evidence(), 2, fact_row())
        compare_result(result, None)
        self.assertEqual(result["reason_code"], "BASELINE_MISSING")

    def test_baseline_strict_profile_layout_and_values(self):
        rows = load_baseline(baseline_text(), profile="unit", nodes=1, gpus_per_node=2)
        self.assertEqual([row.test for row in rows], list(COLLECTIVES))
        self.assertEqual({row.np for row in rows}, {2})
        for text, profile, nodes, gpus in ((baseline_text(), "", 1, 2), (baseline_text(), "unit", 2, 2),
                                           (baseline_text(), "unit", 1, 4),
                                           (baseline_text().replace("error_pct=10", "error_pct=100"), "unit", 1, 2),
                                           (baseline_text().replace("all_reduce=20", "all_reduce=TBD"), "unit", 1, 2),
                                           (baseline_text() + "unknown=1\n", "unit", 1, 2)):
            with self.subTest(profile=profile, nodes=nodes, gpus=gpus, text=text[-40:]), self.assertRaises(BaselineConfigError):
                load_baseline(text, profile=profile, nodes=nodes, gpus_per_node=gpus)

    def test_real_group_parser_validates_receipt_scale_and_output(self):
        group = dict(group_id="group-001", np="2", node_count="1", gpus_per_node="2", nodes="node001")
        write_receipt(self.root, "group-001", section("all_reduce", fixture("rccl-valid.log")).encode())
        result, metrics = rccl_report.parse_group((self.root, group, [fact_row()]))[0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(metrics["actual_np"], 2)
        group["node_count"] = "2"
        self.assertEqual(rccl_report.parse_group((self.root, group, [fact_row()]))[0][0]["reason_code"], "SCALE_MISMATCH")

    def test_missing_group_receipt_cannot_be_pass(self):
        group = dict(group_id="group-001", np="2", node_count="1", gpus_per_node="2", nodes="node001")
        result, _ = rccl_report.parse_group((self.root, group, [fact_row()]))[0]
        self.assertEqual(result["reason_code"], "GROUP_LOG_INVALID")
