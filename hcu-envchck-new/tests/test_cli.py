"""入口校验使用真实 parser/operation/planning；禁止启动远端调度。"""
import contextlib
import io
from unittest.mock import patch

from hcu_envcheck import cli
from hcu_envcheck.models import peer_port
from hcu_envcheck.registry import OPERATIONS, get_operation
from tests.helpers import PROJECT, Workspace


class CliTests(Workspace):
    def invoke(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(arguments))

    def test_every_operation_registered(self):
        for name in OPERATIONS:
            with self.subTest(operation=name):
                operation = get_operation(name)
                self.assertTrue(callable(operation.validate))
                self.assertTrue(callable(operation.stages))
        with self.assertRaises(ValueError):
            get_operation("not-implemented")

    def test_no_arguments_shows_help(self):
        self.assertEqual(self.invoke(), 0)

    def test_version_matches_project_file(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), (PROJECT / "VERSION").read_text(encoding="utf-8").strip())

    def test_dry_run_all_operations_never_calls_controller(self):
        for name in OPERATIONS:
            args = ["per-node-container", name, "-f", str(self.hostfile), "--dry-run"]
            if name != "idle-check":
                args += ["--container", "unit-container", "-i", "unit-image:1"]
            with self.subTest(operation=name), patch("hcu_envcheck.controller.run") as run:
                self.assertEqual(self.invoke(*args), 0)
                run.assert_not_called()
        self.assertFalse((self.work / "output").exists())

    def test_conda_dry_run_and_explicit_target_aliases(self):
        for scenario in ("shared-conda", "node-local-conda"):
            args = [scenario, "rccl", "-f", str(self.hostfile), "--env-script", "/test/env.sh",
                    "--target-python", "/test/python", "--target-workdir", "/test/work", "--dry-run"]
            with self.subTest(scenario=scenario):
                self.assertEqual(self.invoke(*args), 0)

    def test_invalid_common_counts_and_ports(self):
        for field, value in (("concurrency", 0), ("concurrency", 8193), ("group_size", -1),
                             ("gpus_per_node", 0), ("gpus_per_node", 1025), ("timeout", 0),
                             ("ssh_port", 0), ("container_ssh_port", 65536)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                cli.validate_common(self.request(**{field: value}))

    def test_container_required_and_conda_rejects_container_options(self):
        with self.assertRaises(ValueError):
            cli.validate_common(self.request(container=""))
        for scenario in ("shared-conda", "node-local-conda"):
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                cli.validate_common(self.request(scenario=scenario, env_script="/test/env.sh", provided={"container"}))

    def test_paths_control_characters_and_release_scope(self):
        for changes in (dict(env_script="relative.sh"), dict(remote_workdir="/tmp"),
                        dict(remote_workdir="/test/../tmp"), dict(container_python="python -u"),
                        dict(image="bad\nimage"), dict(release=True), dict(hostfile=str(self.work / "missing"))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                cli.validate_common(self.request(**changes))

    def test_foreign_operation_options_rejected(self):
        for key in ("baseline_file", "baseline_profile", "deepep_source", "flux_source"):
            with self.subTest(option=key), self.assertRaises(ValueError):
                cli.validate_common(self.request("platform", provided={key}))

    def test_concurrency_default_and_peer_ports(self):
        args = cli.make_parser().parse_args(["per-node-container", "idle-check", "-f", str(self.hostfile)])
        self.assertEqual(args.concurrency, 1024)
        self.assertNotIn("--keep-remote", cli.make_parser().format_help())
        self.assertEqual(peer_port(self.request(container_ssh_port=26001)), 26001)
        self.assertEqual(peer_port(self.request(scenario="shared-conda", ssh_port=2222)), 2222)

    def test_parameter_error_returns_two_before_dispatch(self):
        with patch("hcu_envcheck.controller.run") as run:
            self.assertEqual(self.invoke("per-node-container", "rccl", "-f", str(self.hostfile),
                                         "--container", "unit", "-i", "image", "--tests", "ag"), 2)
            run.assert_not_called()

    def test_explicit_default_option_is_still_validated(self):
        self.assertEqual(self.invoke("per-node-container", "platform", "-f", str(self.hostfile),
                                     "--container", "unit", "-i", "image", "--group-size=1", "--dry-run"), 2)
