"""DeepEP 支持范围与实际报告解析，混合请求保留可执行项。"""
from hcu_envcheck.dispatch import _config, write_receipt
from hcu_envcheck.operations import deepep
from hcu_envcheck.reporting import deepep_report, checkdeepep_report
from hcu_envcheck.reporting.tables import read_table, table_text
from tests.helpers import RUN_ID, Workspace, fact_row, fixture, section


class DeepEpTests(Workspace):
    def test_single_node_supported_gpu_counts(self):
        for gpus in (2, 4, 8):
            with self.subTest(gpus=gpus):
                request = self.request("deepep", gpus_per_node=gpus)
                deepep.validate(request)
                self.assertEqual(request.tests, ["intranode"])
                self.assertEqual(deepep.unsupported_reason(request, "intranode"), "")

    def test_multi_node_modes_and_expert_divisibility(self):
        request = self.request("deepep", group_size=2)
        deepep.validate(request)
        self.assertEqual(request.tests, ["lowl", "norm"])
        # 3 * 8 = 24 可整除 288，不能整除 256；逐项判断。
        request = self.request("deepep", group_size=3, tests=["norm", "lowl"])
        deepep.validate(request)
        self.assertTrue(deepep.unsupported_reason(request, "norm"))
        self.assertFalse(deepep.unsupported_reason(request, "lowl"))

    def test_partial_unsupported_request_retained_and_config_explains(self):
        request = self.request("deepep", gpus_per_node=4, tests=["norm", "intranode", "lowl"])
        deepep.validate(request)
        self.assertEqual(deepep.stages(request), ["prepare", "norm", "intranode", "lowl"])
        config = _config(request, RUN_ID)
        self.assertIn("HCU_DEEPEP_RUNNABLE_TESTS=intranode", config)
        self.assertIn("HCU_DEEPEP_UNSUPPORTED_NORM=", config)
        self.assertIn("HCU_DEEPEP_UNSUPPORTED_LOWL=", config)

    def test_all_unsupported_unknown_and_duplicate_modes_rejected(self):
        cases = [dict(group_size=2, gpus_per_node=4), dict(gpus_per_node=3),
                 dict(tests=["unknown"]), dict(tests=["intranode", "intranode"])]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                deepep.validate(self.request("deepep", **changes))

    def parse(self, body, mode="lowl", code=0):
        group = dict(group_id="group-001", node_count="2", gpus_per_node="8", np="16", nodes="node001,node002")
        write_receipt(self.root, "group-001", section(mode, body, code).encode())
        row = fact_row(stage=mode, code=code, reason_code="DEEPEP_EXECUTION_FAILED" if code else "DEEPEP_EXECUTED")
        return deepep_report.parse_group((self.root, group, [row]))

    def lowl_body(self):
        return ("[DEEPEP-LAUNCH] node=node001 nnodes=2 node_rank=0 gpu_workers=8 mode=lowl\n"
                "[DEEPEP-LAUNCH] node=node002 nnodes=2 node_rank=1 gpu_workers=8 mode=lowl\n" +
                "".join(f"[rank {rank}] Dispatch + combine bandwidth: {10 + rank}.0 GB/s\n" for rank in range(16)))

    def test_complete_rank_coverage_uses_worst_bandwidth(self):
        results, metrics, _ = self.parse(self.lowl_body())
        self.assertEqual(results[0]["status"], "PASS")
        self.assertEqual(metrics[0]["bandwidth_gbps"], 10)
        self.assertEqual(metrics[0]["metric_samples"], 16)

    def test_missing_rank_or_wrong_worker_count_rejected(self):
        for body, reason in ((self.lowl_body().replace("[rank 15] Dispatch + combine bandwidth: 25.0 GB/s\n", ""), "METRIC_COVERAGE_INCOMPLETE"),
                             (self.lowl_body().replace("gpu_workers=8", "gpu_workers=4"), "LAUNCH_EVIDENCE_INVALID")):
            with self.subTest(reason=reason):
                self.assertEqual(self.parse(body)[0][0]["reason_code"], reason)

    def test_dependency_correctness_oom_and_unsupported_errors_classified(self):
        for text, reason in (("ImportError: libmissing.so", "DEPENDENCY_MISSING"),
                             ("TypeError incompatible function arguments", "DEPENDENCY_API_MISMATCH"),
                             ("AssertionError", "CORRECTNESS_FAILED"), ("HIP out of memory", "OUT_OF_MEMORY"),
                             ("[DEEPEP-UNSUPPORTED] four cards not supported", "DEEPEP_UNSUPPORTED_CONFIG")):
            with self.subTest(reason=reason):
                self.assertEqual(self.parse(text, code=1)[0][0]["reason_code"], reason)

    def test_low_bandwidth_alone_is_not_execution_failure(self):
        body = self.lowl_body()
        for rank in range(16):
            body = body.replace(f": {10 + rank}.0 GB/s", ": 0.1 GB/s")
        self.assertEqual(self.parse(body)[0][0]["status"], "PASS")

    def test_prepare_requires_each_node_atomic_check_result(self):
        from hcu_envcheck.reporting.tables import atomic_text
        group = dict(group_id="group-001", node_count="2", gpus_per_node="8", np="16", nodes="node001,node002")
        atomic_text(self.root / "logs/prepare/group-001.stdout.log", "__HCU_DEEPEP_ENV__\tnode001\t0\n")
        results, _, _ = deepep_report.parse_group((self.root, group, [fact_row(stage="prepare")]))
        self.assertEqual(results[0]["reason_code"], "CHECK_OUTPUT_INCOMPLETE")

    def test_checkdeepep_four_sections_and_exit_code_conflict(self):
        log = self.root / "check.log"
        row = fact_row(target="node-001", stage="checkdeepep", reason_code="CHECK_OK", log="check.log")
        for text, reason in ((fixture("checkdeepep.log"), "CHECK_OK"),
                             (fixture("checkdeepep.log").replace("[PASS] verbs", "[WARN] verbs"), "CHECK_OUTPUT_CONFLICT"),
                             (fixture("checkdeepep.log").split("==== 4.")[0], "CHECK_OUTPUT_INCOMPLETE")):
            with self.subTest(reason=reason):
                log.write_text(text, encoding="utf-8")
                (self.root / "results.tsv").write_text(table_text(list(row), [row]), encoding="utf-8")
                checkdeepep_report.generate(self.root)
                self.assertEqual(read_table(self.root / "results.tsv")[0]["reason_code"], reason)
                self.assertEqual(len(read_table(self.root / "checks.tsv")), 4)
