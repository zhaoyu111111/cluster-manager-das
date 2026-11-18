"""宿主空闲证据解析、判定和逐节点报告；不读取实际硬件。"""
from hcu_envcheck.operations import idle_check
from hcu_envcheck.reporting.idle import evaluate_idle, parse_capture, parse_metrics, parse_pids, parse_sample
from hcu_envcheck.reporting import idle_report
from tests.helpers import RUN_ID, Workspace, fact_row, idle_sample, node_result, write_json


class IdleTests(Workspace):
    def test_idle_busy_and_fractional_usage(self):
        for vram, util, expected in ((0, 0, "IDLE"), (1, 0, "BUSY"), (0, 1, "BUSY"), (0.1, 0, "BUSY")):
            with self.subTest(vram=vram, util=util):
                evidence = parse_sample(idle_sample(vram, util), node="node001", scope="host")
                self.assertEqual(evaluate_idle(evidence, operation="idle-check")["status"], expected)

    def test_invalid_samples_are_unknown(self):
        samples = [idle_sample(code=1), idle_sample(count=0), idle_sample(raw="unrecognized output"),
                   idle_sample().replace("HCU_IDLE_END\t1", ""),
                   idle_sample().replace("\tmetrics\t0\t", "\tmetrics\t255\t%%%")]
        for sample in samples:
            with self.subTest(sample=sample[:50]):
                evidence = parse_sample(sample, node="node001", scope="host")
                self.assertEqual(evaluate_idle(evidence, operation="idle-check")["status"], "UNKNOWN")

    def test_non_host_scope_not_accepted(self):
        evidence = parse_sample(idle_sample(), node="node001", scope="container")
        self.assertEqual(evaluate_idle(evidence, operation="idle-check")["status"], "UNKNOWN")

    def test_malformed_duplicate_and_out_of_range_cards_rejected(self):
        header = "Card Temp Power Clock Fan VRAM% HCU%\n"
        for rows in ("0 30 20 100 0 101% 0%\n", "0 30 20 100 0 nan% 0%\n", "0 30\n",
                     "0 30 20 100 0 0% 0%\n0 30 20 100 0 0% 0%\n"):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_metrics(header + rows)

    def test_process_table_empty_unique_and_invalid(self):
        self.assertEqual(parse_pids("No KFD processes"), [])
        self.assertEqual(parse_pids("KFD PID\n42 worker\n41 worker\n42 worker"), [41, 42])
        for text in ("", "failed to query", "123 worker", "PID: invalid"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_pids(text)

    def test_envelope_duplicate_or_wrong_scope_rejected(self):
        for text in (idle_sample() + idle_sample(), idle_sample().replace("\thost\t", "\tcontainer\t")):
            self.assertTrue(parse_capture(text, node="node001", scope="host")["errors"])

    def test_host_operation_parameter_contract(self):
        for scenario in ("per-node-container", "shared-conda", "node-local-conda"):
            with self.subTest(scenario=scenario):
                idle_check.validate(self.request("idle-check", scenario=scenario,
                                                 provided={"release", "hostfile"}, release=True))
        for changes in (dict(provided={"container"}), dict(timeout=86401)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                idle_check.validate(self.request("idle-check", **changes))

    def test_interpret_and_aggregate_nodes_with_missing_result(self):
        from hcu_envcheck.planning import build_plan, write_plan
        request = self.request("idle-check")
        plan = build_plan(request, idle_check)
        write_plan(self.root, request, plan)
        write_json(self.root / "run.json", {"state": "FINISHED"})
        facts = []
        for target in plan.targets:
            text = idle_sample(node=target.leader) if target.leader == "node001" else "missing evidence"
            node = node_result(self.work / "collected", target.leader, files={"idle.log": text})
            result = idle_check.interpret(node, target, "idle-check", self.root, RUN_ID)
            facts.append(fact_row(target=target.id, node=target.leader, stage="idle-check", code=result.exit_code,
                                  status=result.status, reason_code=result.reason_code, log="unit.log"))
        report = idle_report.collect(self.root, request, plan, facts)
        self.assertEqual(idle_report.totals(report)["IDLE"], 1)
        self.assertEqual(idle_report.totals(report)["UNKNOWN"], 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("node002", idle_report.terminal(report))
        self.assertIn("node001", "\n".join(idle_report.markdown(report)))
        self.assertTrue((self.root / "idle-results.tsv").exists())
