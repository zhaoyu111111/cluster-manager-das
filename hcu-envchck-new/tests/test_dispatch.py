"""真实调度与日志回收，传输边界提供节点结果包，不连接节点。"""
import contextlib
import io
import tarfile

from hcu_envcheck import dispatch
from hcu_envcheck.operations import rccl
from hcu_envcheck.planning import build_plan, write_plan
from hcu_envcheck.reporting.tables import read_table
from tests.helpers import FakeTransport, IMAGE_ID, RUN_ID, Workspace, section


class DispatchTests(Workspace):
    def prepare(self, nodes=3, **changes):
        self.hosts(nodes)
        request = self.request(tests=["all_reduce", "broadcast"], concurrency=1, **changes)
        rccl.validate(request)
        plan = build_plan(request, rccl)
        write_plan(self.root, request, plan)
        return request, plan

    def packages(self, plan, failed_group=None):
        packages = {}
        for target in plan.targets:
            for node in target.nodes:
                packages[("check", node)] = {"node.status": "PASS\tPREPARE_READY\t0\tready\n", "image.id": IMAGE_ID}
            body = section("all_reduce", "unit output", 7 if target.id == failed_group else 0, target.id)
            if target.id != failed_group:
                body += section("broadcast", "unit output", 0, target.id)
            packages[("group", target.leader)] = {target.id + ".log": body}
        return packages

    def test_batch_execution_and_gather_interleave_before_next_batch(self):
        request, plan = self.prepare()
        transport = FakeTransport(self.packages(plan))
        with contextlib.redirect_stdout(io.StringIO()):
            _, executed = dispatch._run_batches(request, transport, self.root, RUN_ID, "entry", "group", 10)
        self.assertEqual(executed, {target.leader for target in plan.targets})
        self.assertEqual([call[0] for call in transport.calls], ["fanout", "rcopy"] * 3)
        self.assertEqual([call[1] for call in transport.calls if call[0] == "fanout"], [[t.leader] for t in plan.targets])

    def test_failed_rccl_group_blocks_remaining_tests_other_groups_continue(self):
        request, plan = self.prepare()
        transport = FakeTransport(self.packages(plan, failed_group="group-001"))
        with contextlib.redirect_stdout(io.StringIO()):
            dispatch.run(request, plan, rccl, transport, self.root, RUN_ID)
        rows = {(target.id, stage): read_table(self.root / ".state/facts" / stage / (target.id + ".tsv"))[0]
                for target in plan.targets for stage in plan.stages}
        self.assertEqual(rows[("group-001", "all_reduce")]["reason_code"], "RCCL_EXECUTION_FAILED")
        self.assertEqual(rows[("group-001", "broadcast")]["status"], "BLOCKED")
        self.assertEqual(rows[("group-001", "broadcast")]["reason_code"], "GROUP_SKIPPED")
        for target in plan.targets[1:]:
            self.assertEqual(rows[(target.id, "broadcast")]["status"], "PASS")
        self.assertEqual(len([call for call in transport.calls if call[0] == "fanout" and call[2].endswith(" group")]), 3)

    def test_failed_member_blocks_whole_group_without_regrouping(self):
        request, plan = self.prepare(nodes=4, group_size=2)
        packages = self.packages(plan)
        packages[("check", plan.targets[0].nodes[1])]["node.status"] = "FAIL\tHOST_NOT_IDLE\t68\tbusy\n"
        transport = FakeTransport(packages)
        with contextlib.redirect_stdout(io.StringIO()):
            dispatch.run(request, plan, rccl, transport, self.root, RUN_ID)
        row = read_table(self.root / ".state/facts/all_reduce/group-001.tsv")[0]
        self.assertEqual(row["status"], "BLOCKED")
        group_calls = [call for call in transport.calls if call[0] == "fanout" and call[2].endswith(" group")]
        self.assertEqual([call[1] for call in group_calls], [[plan.targets[1].leader]])
        self.assertEqual(plan.targets[1].nodes, ("node0003", "node0004"))

    def test_missing_gather_retries_only_missing_nodes_once(self):
        request, plan = self.prepare(nodes=2)
        packages = {("check", plan.targets[0].leader): {"node.status": "PASS\tOK\t0\tready\n"}}
        transport = FakeTransport(packages)
        dispatch._gather(transport, self.root, self.root / "plan/all.hosts", "/test/unit", "check")
        calls = [call for call in transport.calls if call[0] == "rcopy"]
        self.assertEqual(calls[0][1], ["node0001", "node0002"])
        self.assertEqual(calls[1][1], ["node0002"])
        node = dispatch._node_result(self.root / "collected/check", "node0002")
        self.assertEqual(node.reason, "NODE_NO_RESULT")

    def test_corrupt_tar_is_error_evidence_not_pass(self):
        request, _ = self.prepare(nodes=1)
        directory = self.root / "collected/check"
        directory.mkdir(parents=True)
        (directory / "out.tgz.node0001").write_bytes(b"not a tar archive")
        dispatch._gather(FakeTransport(), self.root, self.root / "plan/all.hosts", "/test/unit", "check")
        self.assertTrue((directory / "node0001.error").is_file())
        self.assertEqual(dispatch._node_result(directory, "node0001").status, "FAIL")

    def test_cancel_prevents_unstarted_batches(self):
        request, _ = self.prepare()
        transport = FakeTransport()
        transport.cancelled.set()
        with contextlib.redirect_stdout(io.StringIO()):
            _, executed = dispatch._run_batches(request, transport, self.root, RUN_ID, "entry", "group", 10)
        self.assertEqual(executed, set())
        self.assertEqual(transport.calls, [])

    def test_missing_batch_plan_fails_explicitly(self):
        with self.assertRaises(ValueError):
            dispatch._run_batches(self.request(), FakeTransport(), self.root, RUN_ID, "entry", "group", 10)

    def test_bundle_contains_normalized_payloads_and_no_controller_request_file(self):
        request, _ = self.prepare(nodes=1)
        path = dispatch._bundle(self.root, request, RUN_ID)
        with tarfile.open(path) as archive:
            names = archive.getnames()
            self.assertIn("payloads/group_run.sh", names)
            self.assertIn("plan/groups/group-001/slotsfile", names)
            self.assertNotIn("plan/request.json", names)
            self.assertEqual(archive.extractfile("RUN_ID").read().decode().strip(), RUN_ID)
            self.assertNotIn(b"\r\n", archive.extractfile("payloads/group_run.sh").read())

    def test_test_fact_timeout_incomplete_and_blocked_are_distinct(self):
        from hcu_envcheck.models import Target
        request, _ = self.prepare(nodes=1)
        target = Target("group-001", ("node0001",))
        cases = [(dict(exit_code=124, error="", text="timeout"), "", "TASK_TIMEOUT"),
                 (dict(exit_code=None, error="truncated", text="partial"), "", "TEST_INCOMPLETE"),
                 (None, "previous failed", "GROUP_SKIPPED"), (None, "", "TEST_NOT_EXECUTED")]
        for selected, skip, reason in cases:
            with self.subTest(reason=reason):
                result = dispatch._test_fact(request, target, "all_reduce", "token", selected, "", skip, 1)
                self.assertEqual(result.reason_code, reason)
