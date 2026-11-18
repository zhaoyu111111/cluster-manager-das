"""释放结果不能由 stop 成功或查询空输出推断；动作脚本另由替身用例覆盖。"""
import json

from hcu_envcheck.models import Target
from hcu_envcheck.operations import idle_check
from tests.helpers import CID, RUN_ID, Workspace, idle_sample, node_result, write_json


class ReleaseTests(Workspace):
    def interpret(self, text, release=True):
        write_json(self.root / "plan/request.json", {"release": release})
        node = node_result(self.work / "collected", files={"idle.log": text})
        result = idle_check.interpret(node, Target("node-001", ("node001",)), "idle-check", self.root, RUN_ID)
        record = json.loads((self.root / "idle/node-001.json").read_text(encoding="utf-8"))
        return result, record

    def test_busy_then_confirmed_idle_is_success(self):
        text = (idle_sample(vram=70) + f"OWNER\t41\t{CID}\tunit-container\tstart-1\t123\n"
                + f"RELEASE\t{CID}\tunit-container\tCONFIRMED\n" + idle_sample())
        result, record = self.interpret(text)
        self.assertEqual((result.status, record["action_status"]), ("PASS", "CONFIRMED"))
        self.assertEqual(record["initial_decision"]["status"], "BUSY")
        self.assertEqual(record["decision"]["status"], "IDLE")
        self.assertEqual(record["owners"][0]["container_id"], CID)
        self.assertEqual(len(record["history"]), 2)

    def test_stop_not_confirmed_even_if_followup_sample_idle(self):
        text = (idle_sample(vram=70) + f"RELEASE\t{CID}\tunit-container\tUNCONFIRMED\n"
                + "IDLE_ERROR\tcontainer inspect failed\n" + idle_sample())
        result, record = self.interpret(text)
        self.assertEqual((result.status, result.reason_code), ("FAIL", "RELEASE_UNCONFIRMED"))
        self.assertEqual(record["action_status"], "UNCONFIRMED")
        self.assertNotEqual(result.exit_code, 0)

    def test_unknown_owner_and_exited_pid_remain_unconfirmed(self):
        for event in ("UNKNOWN\t41\n", "EXITED\t41\n"):
            with self.subTest(event=event):
                result, record = self.interpret(idle_sample(vram=70) + event)
                self.assertEqual(result.reason_code, "RELEASE_UNCONFIRMED")
                self.assertEqual(record["actions"], [])

    def test_stop_confirmed_but_still_busy_not_success(self):
        result, record = self.interpret(idle_sample(vram=70) +
                                        f"RELEASE\t{CID}\tunit-container\tCONFIRMED\n" + idle_sample(vram=10))
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(record["action_status"], "UNCONFIRMED")

    def test_release_not_requested_does_not_invent_actions(self):
        result, record = self.interpret(idle_sample(vram=70), release=False)
        self.assertEqual(result.reason_code, "HOST_NOT_IDLE")
        self.assertEqual(record["action_status"], "NOT_REQUESTED")
        self.assertEqual(record["actions"], [])

    def test_already_idle_needs_no_stop_receipt(self):
        result, record = self.interpret(idle_sample())
        self.assertEqual(result.status, "PASS")
        self.assertEqual(record["actions"], [])
