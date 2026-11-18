"""原始事实、日志身份、派生报告和原因码的公共契约。"""
import contextlib
import io
import json

from hcu_envcheck.dispatch import publish_stages, save_fact, write_receipt
from hcu_envcheck.models import Plan, Target
from hcu_envcheck.reporting import engine
from hcu_envcheck.reporting.group_log import read_group, read_test
from hcu_envcheck.reporting.metrics import number
from hcu_envcheck.reporting.tables import atomic_text, read_table, table_text
from tests.helpers import RUN_ID, Workspace, fact, section, write_json


class ReportingTests(Workspace):
    def test_log_receipt_hash_token_and_exit_code(self):
        result = fact()
        data = section("all_reduce", "native output")
        write_receipt(self.root, "group-001", data.encode())
        sections, error = read_group(self.root, "group-001")
        self.assertEqual(error, "")
        self.assertEqual(read_test(sections, error, {"stage": result.stage, "token": result.token,
                                                   "transport_exit_code": 0})[1], "")
        self.assertTrue(read_test(sections, error, {"stage": result.stage, "token": "wrong", "transport_exit_code": 0})[1])
        self.assertTrue(read_test(sections, error, {"stage": result.stage, "token": result.token, "transport_exit_code": 7})[1])
        (self.root / "logs/groups/group-001.log").write_text(data + "tampered", encoding="utf-8")
        self.assertTrue(read_group(self.root, "group-001")[1])

    def test_duplicate_begin_missing_end_and_wrong_end_token(self):
        token = f"{RUN_ID}_group-001_all_reduce"
        begin = f"[TEST-BEGIN] test=all_reduce token={token}\n"
        cases = [begin + "partial", begin + section("all_reduce", "duplicate"),
                 begin + "[TEST-END] test=all_reduce token=wrong exit_code=0\n"]
        for text in cases:
            with self.subTest(text=text):
                write_receipt(self.root, "group-001", text.encode())
                sections, error = read_group(self.root, "group-001")
                self.assertTrue(error or sections[("all_reduce", token)]["error"])

    def test_missing_collection_receipt_never_passes(self):
        self.assertTrue(read_group(self.root, "group-001")[1])
        write_receipt(self.root, "group-001", None, "copy failed")
        self.assertEqual(read_group(self.root, "group-001")[1], "copy failed")

    def test_fact_identity_missing_log_and_false_pass(self):
        result = fact(target="node-001", stage="inspect")
        save_fact(self.root, result)
        job = (self.root, result.target_id, result.node, [result.stage])
        self.assertEqual(engine.read_target(job)[0]["status"], "PASS")
        path = self.root / ".state/facts/inspect/node-001.tsv"
        original = path.read_text(encoding="utf-8")
        for replacement in (original.replace("node001", "other-node"), original.replace("PASS", "UNKNOWN_STATUS")):
            with self.subTest(replacement=replacement[:40]):
                path.write_text(replacement, encoding="utf-8")
                self.assertEqual(engine.read_target(job)[0]["reason_code"], "INVALID_EVIDENCE")
        path.write_text(original, encoding="utf-8")
        (self.root / "logs/inspect/node-001.stderr.log").unlink()
        self.assertEqual(engine.read_target(job)[0]["reason_code"], "INVALID_EVIDENCE")

    def test_missing_fact_is_incomplete(self):
        row = engine.read_target((self.root, "node-001", "node001", ["inspect"]))[0]
        self.assertEqual((row["status"], row["reason_code"]), ("INCOMPLETE", "MISSING_FACT"))

    def test_report_rejects_open_execution(self):
        with self.assertRaisesRegex(ValueError, "执行尚未关闭"):
            engine.generate(self.root, self.request("container-status"), Plan([], ["inspect"]))

    def report(self, status="PASS", code=0, receipt=True, skipped=None):
        result = fact(target="node-001", stage="inspect", status=status, code=code)
        save_fact(self.root, result)
        if receipt:
            publish_stages(self.root, "node-001", ["inspect"])
        else:
            write_receipt(self.root, "node-001", None, "copy failed")
        atomic_text(self.root / ".state/execution.closed", RUN_ID)
        write_json(self.root / "run.json", {"state": "FINISHED"})
        plan = Plan([Target("node-001", ("node001",))], ["inspect"], skipped_nodes=skipped or [])
        request = self.request("container-status", group_size=4)
        with contextlib.redirect_stdout(io.StringIO()):
            result_code = engine.generate(self.root, request, plan)
        return result_code, json.loads((self.root / "results.json").read_text(encoding="utf-8"))

    def test_pass_warn_fail_summary_exit_codes(self):
        for status, execution_code, expected in (("PASS", 0, 0), ("WARN", 0, 2), ("FAIL", 7, 1)):
            with self.subTest(status=status):
                code, report = self.report(status, execution_code)
                self.assertEqual((code, report["status"]), (expected, status))

    def test_collection_failure_changes_derived_results_not_execution(self):
        code, report = self.report(receipt=False)
        self.assertEqual(code, 1)
        self.assertEqual(report["results"][0]["reason_code"], "LOG_COLLECTION_FAILED")
        execution = read_table(self.root / "execution.tsv")[0]
        self.assertEqual((execution["status"], execution["exit_code"]), ("PASS", "0"))
        self.assertEqual(read_table(self.root / ".state/facts/inspect/node-001.tsv")[0]["status"], "PASS")

    def test_tail_nodes_reported_without_counting_as_executed(self):
        _, report = self.report(skipped=["node002", "node003"])
        self.assertEqual([row["node"] for row in report["skipped_nodes"]], ["node002", "node003"])
        self.assertIn("不足 group-size=4", report["skipped_nodes"][0]["reason"])
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn("跳过节点", (self.root / "summary.md").read_text(encoding="utf-8"))

    def test_table_escaping_atomic_write_and_nonfinite_metrics(self):
        atomic_text(self.root / "nested/text", "first")
        atomic_text(self.root / "nested/text", "second")
        self.assertEqual((self.root / "nested/text").read_text(encoding="utf-8"), "second")
        text = table_text(["node", "detail"], [{"node": "node001", "detail": "a\tb\nc"}])
        (self.root / "table.tsv").write_text(text, encoding="utf-8")
        self.assertEqual(read_table(self.root / "table.tsv")[0]["node"], "node001")
        self.assertEqual(engine.markdown_cell("a|<x>\ny"), "a\\|&lt;x&gt; y")
        for value in ("nan", "inf", "-inf", None, "bad"):
            self.assertIsNone(number(value))
