"""分组、尾组与 clushhostfile 批次规则，包含 1000 节点数据。"""
import contextlib
import io
import json

from hcu_envcheck.planning import build_plan, preview, read_hosts, write_plan
from hcu_envcheck.registry import get_operation
from tests.helpers import Workspace


class PlanningTests(Workspace):
    def plan(self, request):
        operation = get_operation(request.operation)
        operation.validate(request)
        return build_plan(request, operation)

    def test_hostfile_bom_comments_and_order(self):
        self.hostfile.write_text("\ufeff# comment\nnode003 # first\n\nnode001\n", encoding="utf-8")
        self.assertEqual(read_hosts(str(self.hostfile)), ["node003", "node001"])

    def test_hostfile_invalid_inputs(self):
        for text in ("", "# only comment", "node001\nnode001\n", "node001 slots=8\n", "-bad\n", "node[1-3]\n"):
            with self.subTest(text=text):
                self.hostfile.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_hosts(str(self.hostfile))

    def test_single_dual_quad_groups_and_tail(self):
        nodes = self.hosts(9)
        for size, groups, skipped in ((1, 9, 0), (2, 4, 1), (4, 2, 1)):
            with self.subTest(size=size):
                plan = self.plan(self.request(group_size=size))
                self.assertEqual(len(plan.targets), groups)
                self.assertEqual(plan.skipped_nodes, nodes[len(nodes) - skipped:] if skipped else [])
                self.assertEqual([target.leader for target in plan.targets], nodes[:groups * size:size])

    def test_insufficient_nodes_is_parameter_error(self):
        with self.assertRaisesRegex(ValueError, "没有可执行的完整分组"):
            self.plan(self.request(group_size=4))

    def test_node_operations_have_one_target_per_node(self):
        for name in ("platform", "idle-check", "checkdeepep", "byte-flux", "container-create"):
            with self.subTest(operation=name):
                plan = self.plan(self.request(name))
                self.assertFalse(plan.grouped)
                self.assertEqual([target.nodes for target in plan.targets], [("node001",), ("node002",)])

    def test_1000_nodes_250_groups_two_batches(self):
        nodes = self.hosts(1000)
        request = self.request(group_size=4, concurrency=128)
        plan = self.plan(request)
        write_plan(self.root, request, plan)
        batches = sorted((self.root / "plan").glob("clushhostfile.*"))
        self.assertEqual([len(path.read_text(encoding="utf-8").splitlines()) for path in batches], [128, 122])
        self.assertEqual([node for path in batches for node in path.read_text(encoding="utf-8").splitlines()], nodes[::4])
        self.assertEqual((self.root / "plan/all.hosts").read_text(encoding="utf-8").splitlines(), nodes)

    def test_per_node_batches_no_silent_64_limit(self):
        self.hosts(130)
        request = self.request("container-status", concurrency=100)
        plan = self.plan(request)
        write_plan(self.root, request, plan)
        batches = sorted((self.root / "plan").glob("clushhostfile.*"))
        self.assertEqual([len(path.read_text(encoding="utf-8").splitlines()) for path in batches], [100, 30])

    def test_skipped_tail_excluded_from_all_hosts_and_slots(self):
        nodes = self.hosts(5)
        request = self.request(group_size=2, gpus_per_node=4)
        plan = self.plan(request)
        write_plan(self.root, request, plan)
        self.assertEqual((self.root / "plan/skipped.hosts").read_text(encoding="utf-8").splitlines(), [nodes[-1]])
        self.assertNotIn(nodes[-1], (self.root / "plan/all.hosts").read_text(encoding="utf-8"))
        slots = (self.root / "plan/groups/group-001/slotsfile").read_text(encoding="utf-8")
        self.assertEqual(slots.splitlines(), [f"{node} slots=4" for node in nodes[:2]])

    def test_deepep_one_mpi_slot_per_node_and_np_gpu_count(self):
        request = self.request("deepep", group_size=2, gpus_per_node=8)
        write_plan(self.root, request, self.plan(request))
        self.assertIn("node001 slots=1", (self.root / "plan/groups/group-001/slotsfile").read_text(encoding="utf-8"))
        from hcu_envcheck.reporting.tables import read_table
        self.assertEqual(read_table(self.root / "plan/groups.tsv")[0]["np"], "16")

    def test_preview_does_not_write_plan_or_expand_all_nodes(self):
        self.hosts(1000)
        request = self.request()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            preview(request, self.plan(request))
        self.assertFalse((self.root / "plan").exists())
        self.assertLess(len(output.getvalue().splitlines()), 30)

    def test_request_persisted_without_environment(self):
        request = self.request(provided={"hostfile", "concurrency"})
        write_plan(self.root, request, self.plan(request))
        data = json.loads((self.root / "plan/request.json").read_text(encoding="utf-8"))
        self.assertEqual(data["provided"], ["concurrency", "hostfile"])
        self.assertNotIn("environ", data)
