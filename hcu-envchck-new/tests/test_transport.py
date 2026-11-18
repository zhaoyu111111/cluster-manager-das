"""在 subprocess 边界使用替身，验证真实 clush 参数和退出码处理。"""
import os
from unittest.mock import Mock, patch

from hcu_envcheck.clush import Transport, subprocess_environment
from hcu_envcheck.concurrency import parallel_map
from tests.helpers import Workspace


class TransportTests(Workspace):
    def test_original_environment_restored(self):
        environment = dict(PATH="private", PYTHONHOME="private", PYTHONPATH="private",
                           HCU_LAUNCH_ORIGINAL_PATH_SET="1", HCU_LAUNCH_ORIGINAL_PATH="original",
                           HCU_LAUNCH_ORIGINAL_PYTHONHOME_SET="0", HCU_LAUNCH_ORIGINAL_PYTHONPATH_SET="1",
                           HCU_LAUNCH_ORIGINAL_PYTHONPATH="", UNRELATED="keep")
        with patch.dict(os.environ, environment, clear=True):
            restored = subprocess_environment()
        self.assertEqual(restored["PATH"], "original")
        self.assertNotIn("PYTHONHOME", restored)
        self.assertEqual(restored["PYTHONPATH"], "")
        self.assertEqual(restored["UNRELATED"], "keep")
        self.assertFalse(any(key.startswith("HCU_LAUNCH_ORIGINAL_") for key in restored))

    def test_clush_single_batch_command_and_connection_options(self):
        transport = Transport(self.request(concurrency=150, ssh_user="unituser", ssh_port=2222,
                                           ssh_config="/test/ssh.conf"))
        process = Mock(returncode=0)
        with patch.object(transport, "_spawn", return_value=process) as spawn:
            self.assertEqual(transport.fanout(self.hostfile, "bash payload.sh", 99, self.work / "clush.log"), 0)
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[argv.index("--hostfile") + 1], str(self.hostfile))
        self.assertEqual(argv[argv.index("-f") + 1], "150")
        self.assertIn("unituser", argv)
        self.assertIn("Port=2222", " ".join(argv))
        self.assertIn("/test/ssh.conf", " ".join(argv))
        self.assertEqual(argv[-1], "bash payload.sh")
        self.assertNotIn(process, transport._processes)

    def test_copy_cancel_policy_differs_from_gather(self):
        transport = Transport(self.request())
        with patch.object(transport, "_fanout", return_value=0) as fanout:
            transport.copy(self.hostfile, self.work / "bundle", "/test/remote", 10, self.work / "copy.log")
            transport.rcopy(self.hostfile, "/test/out.tgz", self.work, 10, self.work / "gather.log")
        self.assertFalse(fanout.call_args_list[0].args[-1])
        self.assertTrue(fanout.call_args_list[1].args[-1])

    def test_cancelled_does_not_start_new_process(self):
        transport = Transport(self.request())
        transport.cancel_all()
        with patch("hcu_envcheck.clush.subprocess.Popen") as popen:
            result = transport.fanout(self.hostfile, "true", 10, self.work / "clush.log")
        popen.assert_not_called()
        self.assertEqual(result, 130)

    def test_batch_exit_code_preserved(self):
        transport = Transport(self.request())
        for code in (0, 7, 124, -15):
            process = Mock(returncode=code)
            transport._processes.add(process)
            with self.subTest(code=code), patch.object(transport, "_spawn", return_value=process):
                result = transport.fanout(self.hostfile, "echo test", 10, self.work / "clush.log")
                self.assertEqual(result, code)
                process.wait.assert_called_once_with()
                self.assertNotIn(process, transport._processes)

    def test_cancel_stops_only_registered_processes(self):
        transport = Transport(self.request())
        processes = {Mock(), Mock()}
        transport._processes.update(processes)
        with patch.object(transport, "_stop") as stop:
            transport.cancel_all()
        self.assertTrue(transport.cancelled.is_set())
        self.assertCountEqual([call.args[0] for call in stop.call_args_list], processes)

    def test_missing_clustershell_reports_requirement(self):
        with patch("hcu_envcheck.clush.importlib.util.find_spec", return_value=None), self.assertRaises(ValueError):
            Transport(self.request()).require()

    def test_parallel_results_order_and_errors(self):
        self.assertEqual(parallel_map(lambda value: value * 2, [3, 1, 2], 2), [6, 2, 4])
        self.assertEqual(parallel_map(lambda value: value, [], 2), [])
        def fail(value):
            raise ValueError("worker failed")
        with self.assertRaisesRegex(ValueError, "worker failed"):
            parallel_map(fail, [1], 1)
