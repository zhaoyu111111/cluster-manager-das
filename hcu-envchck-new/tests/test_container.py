"""容器动作声明及回传证据校验；Docker 动作由离线 Shell 用例覆盖。"""
from hcu_envcheck.models import Target
from hcu_envcheck.operations import container
from tests.helpers import CID, IMAGE_ID, RUN_ID, Workspace, node_result


class ContainerTests(Workspace):
    def test_four_stage_sequences(self):
        expected = {"container-status": ["inspect"], "container-create": ["preflight", "create", "ssh-identity"],
                    "container-recreate": ["preflight", "replace", "create", "ssh-identity"],
                    "container-delete": ["preflight", "delete"]}
        for name, stages in expected.items():
            with self.subTest(operation=name):
                request = self.request(name, yes=name in {"container-recreate", "container-delete"})
                container.validate(request)
                self.assertEqual(container.stages(request), stages)

    def test_delete_and_recreate_need_confirmation_except_dry_run(self):
        for name in ("container-delete", "container-recreate"):
            with self.subTest(operation=name), self.assertRaises(ValueError):
                container.validate(self.request(name))
            container.validate(self.request(name, dry_run=True))

    def test_foreign_parameters_and_conflicting_docker_options(self):
        for request in (self.request("container-create", provided={"gpus_per_node"}),
                        self.request("container-create", docker_args=["--name=other"]),
                        self.request("container-create", docker_args=["--privileged=false"]),
                        self.request("container-status", provided={"port"}),
                        self.request("container-create", image="")):
            with self.subTest(request=request), self.assertRaises(ValueError):
                container.validate(request)
        container.validate(self.request("container-create", docker_args=["--init", "--cpus=2"]))

    def valid_node(self, stage="inspect", status="PASS", code=0):
        token = f"{RUN_ID}_node-001_{stage}"
        files = {stage + ".status": f"{status}\tOK\t{code}\tunit evidence\n",
                 stage + ".meta": "\n".join([RUN_ID, "node-001", stage, token, CID, IMAGE_ID, "10", "11"]) + "\n",
                 stage + ".log": "output", stage + ".stderr.log": ""}
        return node_result(self.root, files=files)

    def interpret(self, node, stage="inspect"):
        return container.interpret(node, Target("node-001", ("node001",)), stage, self.root, RUN_ID)

    def test_valid_stage_preserves_full_identity(self):
        result = self.interpret(self.valid_node())
        self.assertEqual((result.status, result.container_id, result.image_id), ("PASS", CID, IMAGE_ID))
        self.assertEqual((result.started_epoch, result.ended_epoch), (10, 11))

    def test_missing_stage_and_transport_failure(self):
        node = node_result(self.root, status="FAIL", reason="NODE_NO_RESULT", code=255)
        result = self.interpret(node)
        self.assertEqual((result.status, result.reason_code, result.exit_code), ("FAIL", "NODE_NO_RESULT", 255))

    def test_forged_meta_missing_log_and_false_pass_rejected(self):
        cases = {"inspect.meta": "wrong-run\n", "inspect.status": "PASS\tOK\t7\twrong\n",
                 "inspect.stderr.log": None}
        for name, value in cases.items():
            with self.subTest(file=name):
                node = self.valid_node()
                path = node.results / name
                if value is None:
                    path.unlink()
                else:
                    path.write_text(value, encoding="utf-8")
                self.assertEqual(self.interpret(node).reason_code, "CONTAINER_STAGE_INVALID")

    def test_warn_and_blocked_are_preserved(self):
        for status, code in (("WARN", 0), ("BLOCKED", 1)):
            with self.subTest(status=status):
                result = self.interpret(self.valid_node(status=status, code=code))
                self.assertEqual((result.status, result.exit_code), (status, code))
