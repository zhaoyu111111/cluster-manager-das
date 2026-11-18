"""执行原 Shell 逻辑，但 SSH/Docker/MPI/目标 Python/硬件命令全为离线替身。"""
import json
import unittest

from hcu_envcheck.reporting.platform_report import parse_payload
from tests.helpers import CID, IMAGE_ID, RUN_ID, Workspace, fixture
from tests.shell_support import PayloadJob, SHELL_AVAILABLE


@unittest.skipUnless(SHELL_AVAILABLE, "原 Shell 离线用例需要 Linux、bash 和 timeout；不在 Windows 上冒充验证")
class PayloadTests(Workspace):
    def test_first_ssh_known_hosts_stderr_does_not_break_run_identity(self):
        job = PayloadJob(self)
        job.configure(ssh_stderr="Warning: Permanently added 'node001' to known hosts.\n")
        result = job.run("group_run.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[TEST-END]", job.text("group-001.log"))
        self.assertIn("Permanently added", job.text("group-001.log"))
        self.assertEqual(len(job.calls("mpirun")), 1)

    def test_wrong_ssh_identity_blocks_group(self):
        job = PayloadJob(self)
        job.configure(ssh_output="another-run")
        job.run("group_run.sh")
        self.assertIn("PEER_VERIFICATION_FAILED", job.text("group.status"))
        self.assertEqual(job.calls("mpirun"), [])

    def test_rccl_command_failure_and_timeout_stop_only_remaining_items(self):
        for code in (7, 124):
            with self.subTest(code=code):
                job = PayloadJob(self, tests=["all_reduce", "broadcast", "reduce"])
                job.configure(mpi_exit_by_test={"broadcast": code})
                job.run("group_run.sh")
                log = job.text("group-001.log")
                self.assertIn(f"test=broadcast token={RUN_ID}_group-001_broadcast exit_code={code}", log)
                self.assertNotIn("test=reduce token=", log)
                self.assertEqual(len(job.calls("mpirun")), 2)

    def test_zero_exit_low_bandwidth_does_not_stop_following_item(self):
        job = PayloadJob(self, tests=["all_reduce", "broadcast"])
        job.configure(mpi_output="Avg bus bandwidth : 0.001\n")
        job.run("group_run.sh")
        self.assertEqual(len(job.calls("mpirun")), 2)
        self.assertIn("test=broadcast", job.text("group-001.log"))

    def test_rccl_mpi_scale_slots_port_and_env_script(self):
        env = self.work / "site env.sh"
        env.write_text("export NCCL_TEST_SETTING=unit\n")
        for nodes, gpus in ((1, 4), (2, 4), (4, 8)):
            with self.subTest(nodes=nodes, gpus=gpus):
                job = PayloadJob(self, nodes=nodes, group_size=nodes, gpus_per_node=gpus,
                                 container_ssh_port=26011, env_script=str(env))
                result = job.run("group_run.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = job.calls("mpirun")[0]["argv"]
                self.assertEqual(args[args.index("-np") + 1], str(nodes * gpus))
                self.assertIn("slotsfile", args[args.index("--hostfile") + 1])
                self.assertIn("-p 26011", " ".join(args))
                self.assertIn("NCCL_TEST_SETTING", args)
                self.assertIn("source ", args[-1])

    def test_deepep_mixed_unsupported_modes_still_run_intranode(self):
        job = PayloadJob(self, "deepep", gpus_per_node=4, tests=["norm", "intranode", "lowl"])
        job.run("group_run.sh")
        text = job.text("group-001.log")
        self.assertEqual(text.count("[DEEPEP-UNSUPPORTED]"), 2)
        self.assertIn("mode=intranode", text)
        self.assertEqual(len(job.calls("target-python")), 1)
        self.assertEqual(job.calls("mpirun"), [])

    def test_deepep_multi_node_mpi_one_process_per_node(self):
        job = PayloadJob(self, "deepep", nodes=2, group_size=2, gpus_per_node=8, tests=["norm"])
        job.environment["HCU_GROUP"] = job.target.id
        result = job.run("deepep.sh", "norm")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = job.calls("mpirun")[0]["argv"]
        self.assertEqual(args[args.index("-np") + 1], "2")
        self.assertIn("--num-processes 8", args[-1])
        self.assertIn("--nproc-per-node=1", args[-1])
        self.assertIn("--node-rank=${OMPI_COMM_WORLD_RANK}", args[-1])

    def test_byte_flux_worker_count_and_native_exit_code(self):
        for gpus in (1, 2, 4, 8):
            with self.subTest(gpus=gpus):
                job = PayloadJob(self, "byte-flux", gpus_per_node=gpus)
                job.configure(python_exit=9)
                result = job.run("byte_flux.sh", f"tp{gpus}")
                self.assertEqual(result.returncode, 9)
                self.assertIn(f"--nproc-per-node={gpus}", job.calls("target-python")[0]["argv"])
        job = PayloadJob(self, "byte-flux", gpus_per_node=4)
        self.assertEqual(job.run("byte_flux.sh", "tp8").returncode, 64)
        self.assertEqual(job.calls("target-python"), [])

    def test_idle_gate_zero_busy_and_command_failure(self):
        for values, expected in ((dict(vram_samples=[0]), 0), (dict(vram_samples=[70]), 2),
                                 (dict(smi_exit=1), 1)):
            with self.subTest(values=values):
                job = PayloadJob(self, "idle-check")
                job.configure(**values)
                self.assertEqual(job.run("idle.sh", "gate").returncode, expected)
                self.assertEqual(job.calls("docker"), [])

    def test_release_deduplicates_container_and_does_not_touch_unoccupied_container(self):
        job = PayloadJob(self, "idle-check", release=True)
        job.configure(vram_samples=[70, 0], busy_pids=[600001, 600002], extra_container_ids=["d" * 64],
                      container=dict(id=CID, name="unit-container", exists=True, running=True,
                                     image=IMAGE_ID, started="start-1", pids=[600001, 600002]))
        result = job.run("idle.sh", "run")
        self.assertEqual(result.returncode, 0, result.stderr)
        stops = [row for row in job.calls("docker") if row["argv"][0] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["argv"][-1], CID)
        self.assertIn("\tCONFIRMED\n", result.stdout)
        self.assertFalse(any(row["argv"][0] == "rm" for row in job.calls("docker")))

    def test_release_query_failure_not_confirmed_even_if_resource_idle(self):
        job = PayloadJob(self, "idle-check", release=True)
        job.configure(vram_samples=[70, 0], inspect_after_stop_exit=1)
        result = job.run("idle.sh", "run")
        self.assertIn("\tUNCONFIRMED\n", result.stdout)
        self.assertNotIn("\tCONFIRMED\n", result.stdout)
        self.assertIn("IDLE_ERROR", result.stdout)

    def test_unknown_owner_and_pid_reuse_prevent_stop(self):
        for values in (dict(busy_pids=[600002]), dict(pid_starts={"600001": [123, 456]})):
            with self.subTest(values=values):
                job = PayloadJob(self, "idle-check", release=True)
                job.configure(vram_samples=[70], **values)
                result = job.run("idle.sh", "run")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(row["argv"][0] == "stop" for row in job.calls("docker")))

    def test_release_stop_failure_and_still_busy_remain_failure(self):
        for values in (dict(stop_exit=1), {}):
            with self.subTest(values=values):
                job = PayloadJob(self, "idle-check", release=True)
                job.configure(vram_samples=[70], **values)
                self.assertNotEqual(job.run("idle.sh", "run").returncode, 0)

    def test_no_release_flag_never_stops_busy_container(self):
        job = PayloadJob(self, "idle-check")
        job.configure(vram_samples=[70])
        self.assertEqual(job.run("idle.sh", "run").returncode, 2)
        self.assertFalse(any(row["argv"][0] == "stop" for row in job.calls("docker")))

    def test_container_four_actions_execute_original_stage_workflow(self):
        for operation in ("container-create", "container-status", "container-recreate", "container-delete"):
            with self.subTest(operation=operation):
                job = PayloadJob(self, operation, yes=operation in ("container-delete", "container-recreate"))
                if operation == "container-create":
                    job.configure(container=dict(id=CID, name="unit-container", exists=False, running=False,
                                                 image=IMAGE_ID, started="start-1", pids=[]))
                result = job.run("container.sh")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr + job.text("node.status"))
                for stage in job.plan.stages:
                    self.assertTrue(job.text(stage + ".status").startswith("PASS\t"), stage)
                if operation in ("container-create", "container-recreate"):
                    self.assertIn("ssh_identity", job.state())

    def test_container_preflight_failure_blocks_local_remaining_stages(self):
        job = PayloadJob(self, "container-create")
        job.configure(container=dict(id=CID, name="unit-container", exists=False, running=False,
                                     image=IMAGE_ID, started="start-1", pids=[]), port_busy=True)
        self.assertNotEqual(job.run("container.sh").returncode, 0)
        self.assertIn("PORT_BUSY", job.text("preflight.status"))
        self.assertTrue(job.text("create.status").startswith("BLOCKED\t"))
        self.assertTrue(job.text("ssh-identity.status").startswith("BLOCKED\t"))
        self.assertFalse(any("-dit" in row["argv"] for row in job.calls("docker")))

    def test_container_identity_changed_since_preflight_refuses_delete(self):
        job = PayloadJob(self, "container-delete", yes=True)
        self.assertEqual(job.run("container.sh", "preflight").returncode, 0)
        changed = job.state()["container"]
        changed["id"] = "e" * 64
        job.configure(container=changed)
        self.assertNotEqual(job.run("container.sh", "delete").returncode, 0)
        self.assertIn("CONTAINER_ID_CHANGED", job.text("delete.status"))
        self.assertFalse(any(row["argv"][0] in ("stop", "rm") for row in job.calls("docker")))

    def test_stopped_container_status_warn_and_loopback_wrong_token_fails(self):
        job = PayloadJob(self, "container-status")
        job.configure(container=dict(id=CID, name="unit-container", exists=True, running=False,
                                     image=IMAGE_ID, started="start-1", pids=[]))
        job.run("container.sh")
        self.assertIn("WARN\tCONTAINER_STOPPED", job.text("inspect.status"))
        job = PayloadJob(self, "container-recreate", yes=True)
        job.configure(ssh_identity_output="wrong token")
        self.assertNotEqual(job.run("container.sh").returncode, 0)
        self.assertIn("SSH_IDENTITY_FAILED", job.text("ssh-identity.status"))

    def test_target_prechecks_block_missing_dependency_and_gpu(self):
        for operation, values, reason in (("byte-flux", dict(dependency_exit=66), "DEPENDENCY_MISSING"),
                                           ("rccl", dict(gpu_exit=69), "GPU_CHECK_FAILED")):
            with self.subTest(operation=operation):
                job = PayloadJob(self, operation)
                job.configure(**values)
                self.assertNotEqual(job.run("node_check.sh").returncode, 0)
                self.assertIn(reason, job.text("node.status"))
                self.assertEqual(job.calls("mpirun"), [])

    def test_platform_node_script_wraps_one_payload_with_matching_token(self):
        job = PayloadJob(self, "platform")
        job.configure(platform_payload=json.loads(fixture("platform.json")))
        result = job.run("node_run.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(parse_payload(result.stdout, f"{RUN_ID}_node-001_platform"))
        self.assertIn("PLATFORM_COLLECTED", job.text("node.status"))

    def test_env_source_failure_blocks_group_before_mpi(self):
        env = self.work / "failing env.sh"
        env.write_text("return 7\n")
        job = PayloadJob(self, env_script=str(env))
        job.run("group_run.sh")
        self.assertIn("ENV_SCRIPT_FAILED", job.text("group.status"))
        self.assertEqual(job.calls("mpirun"), [])

    def test_entry_rejects_wrong_node_and_image_before_target_execution(self):
        job = PayloadJob(self)
        job.configure(hostname="other-node")
        job.run("entry.sh", "check")
        self.assertIn("NODE_IDENTITY_FAILED", job.text("node.status"))
        job = PayloadJob(self)
        job.configure(container=dict(id=CID, name="unit-container", exists=True, running=True,
                                     image="sha256:" + "f" * 64, started="start-1", pids=[]))
        job.run("entry.sh", "check")
        self.assertIn("IMAGE_MISMATCH", job.text("node.status"))
        self.assertEqual(job.calls("target-python"), [])
