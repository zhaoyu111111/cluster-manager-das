"""源码/安装布局的同一启动器；只执行临时解释器替身，不构建安装包。"""
import os
import shutil
import subprocess
import sys
import unittest

from tests.helpers import PROJECT, Workspace
from tests.shell_support import SHELL_AVAILABLE


@unittest.skipUnless(SHELL_AVAILABLE, "启动器和安装目录标记用例需要 Linux/bash")
class LauncherTests(Workspace):
    def setUp(self):
        super().setUp()
        self.layout = self.work / "installation"
        (self.layout / "bin").mkdir(parents=True)
        self.launcher = self.layout / "bin/hcu-cluster-run"
        self.launcher.write_bytes((PROJECT / "bin/hcu-cluster-run").read_bytes().replace(b"\r\n", b"\n"))
        self.launcher.chmod(0o700)
        self.environment = dict(os.environ)
        for key in ("HCU_CONTROL_RUNTIME", "HCU_CONTROL_PYTHON", "PYTHONHOME", "PYTHONPATH"):
            self.environment.pop(key, None)

    def fake_python(self, path, label):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\nprintf 'chosen=" + label + "\\n'\nprintf '<%s>\\n' \"$@\"\n"
                        "printf 'cwd=%s\\npythonpath=%s\\n' \"$PWD\" \"$PYTHONPATH\"\n")
        path.chmod(0o700)
        return path

    def run_launcher(self, *args, launcher=None):
        return subprocess.run(["bash", str(launcher or self.launcher), *args], cwd=self.work,
                              env=self.environment, capture_output=True, text=True, timeout=10)

    def test_source_uses_explicit_control_python_and_keeps_cwd(self):
        python = self.fake_python(self.work / "source-python", "source")
        self.environment["HCU_CONTROL_PYTHON"] = str(python)
        result = self.run_launcher("per-node-container", "idle-check", "-f", "relative.hosts")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chosen=source", result.stdout)
        self.assertIn("cwd=" + str(self.work), result.stdout)
        self.assertIn("pythonpath=" + str(self.layout), result.stdout)
        self.assertIn("<relative.hosts>", result.stdout)
        self.assertIn("hcu_envcheck/__main__.py", result.stdout)

    def test_embedded_runtime_has_priority_over_external_python(self):
        self.fake_python(self.layout / "runtime/bin/python3", "embedded")
        self.environment["HCU_CONTROL_PYTHON"] = str(self.fake_python(self.work / "external", "external"))
        result = self.run_launcher("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chosen=embedded", result.stdout)

    def test_external_runtime_fallback(self):
        runtime = self.work / "private-runtime"
        self.fake_python(runtime / "bin/python3", "private")
        self.environment["HCU_CONTROL_RUNTIME"] = str(runtime)
        self.assertIn("chosen=private", self.run_launcher("--help").stdout)

    def test_missing_or_nonexecutable_runtime_has_explicit_error(self):
        self.assertEqual(self.run_launcher("--help").returncode, 69)
        self.environment["HCU_CONTROL_PYTHON"] = str(self.work / "missing-python")
        self.assertEqual(self.run_launcher("--help").returncode, 69)

    def test_symlink_and_clush_subcommand_resolve_same_installation(self):
        self.fake_python(self.layout / "runtime/bin/python3", "embedded")
        link = self.work / "hcu-command"
        link.symlink_to(self.launcher)
        result = self.run_launcher("clush", "-w", "node001", "hostname", launcher=link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hcu_envcheck/clush_entry.py", result.stdout)
        self.assertNotIn("<clush>", result.stdout)
        self.assertIn("<node001>", result.stdout)

    def test_user_install_marker_accepts_only_matching_prefix_and_user(self):
        common = self.work / "user_install_common.sh"
        common.write_bytes((PROJECT / "build/user_install_common.sh").read_bytes().replace(b"\r\n", b"\n"))
        base = self.work / "install-base"
        base.mkdir()
        prefix = self.work / "prefix"
        marker = base / "INSTALLATION"
        script = 'source "$1"; hcu_base=$2; hcu_prefix=$3; hcu_validate_installation'
        marker.write_text(f"hcu-envcheck-user-install-v1\n{prefix}\n{os.geteuid()}\n")
        command = ["bash", "-c", script, "hcu-unit", str(common), str(base), str(prefix)]
        self.assertEqual(subprocess.run(command, capture_output=True, timeout=10).returncode, 0)
        marker.write_text(f"hcu-envcheck-user-install-v1\n/wrong-prefix\n{os.geteuid()}\n")
        self.assertNotEqual(subprocess.run(command, capture_output=True, timeout=10).returncode, 0)

    def run_installed_uninstall(self, *arguments):
        prefix = self.work / "user prefix"
        base = prefix / "share/hcu-envcheck"
        release = base / "releases/test/bin"
        release.mkdir(parents=True)
        (release / "hcu-cluster-run").write_text("test release\n")
        (base / "config").mkdir()
        (base / "config/env.sh").write_text("# user configuration\n")
        (base / "INSTALLATION").write_text(f"hcu-envcheck-user-install-v1\n{prefix}\n{os.geteuid()}\n")
        for source, target in (("uninstall.sh", "uninstall.sh"),
                               ("build/user_install_common.sh", "install-common.sh")):
            (base / target).write_bytes((PROJECT / source).read_bytes().replace(b"\r\n", b"\n"))
        (base / "current").symlink_to("releases/test")
        (prefix / "bin").mkdir()
        (prefix / "bin/hcu-cluster-run").symlink_to(release / "hcu-cluster-run")
        stub_bin = self.work / "uninstall-bin"
        stub_bin.mkdir()
        # 模拟共享文件系统拒绝删除仍被卸载进程打开的文件，同时检查锁未提前释放。
        remover = stub_bin / "rm"
        real_rm = shutil.which("rm")
        remover.write_text(f'''#!{sys.executable}
import fcntl
import os
import sys

base = os.environ["HCU_TEST_INSTALL_BASE"]
if any(arg in (base, base + "/releases") for arg in sys.argv[1:]):
    parent = f"/proc/{{os.getppid()}}"
    for descriptor in os.scandir(parent + "/fd"):
        try:
            target = os.readlink(descriptor.path)
        except FileNotFoundError:
            continue
        if target == base or target.startswith(base + "/"):
            sys.exit("installation file still open: " + target)
    cwd = os.readlink(parent + "/cwd")
    if cwd == base or cwd.startswith(base + "/"):
        sys.exit("uninstaller still inside installation directory")
    with open(os.environ["HCU_TEST_INSTALL_LOCK"], "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            sys.exit("installation lock released before deletion")
    print("[UNINSTALL-DELETE-CHECK] PASS", flush=True)
os.execv({real_rm!r}, [{real_rm!r}, *sys.argv[1:]])
''')
        remover.chmod(0o700)
        environment = dict(self.environment, PATH=str(stub_bin) + os.pathsep + os.environ["PATH"],
                           HCU_TEST_INSTALL_BASE=str(base),
                           HCU_TEST_INSTALL_LOCK=str(prefix / "share/.hcu-envcheck.install.lock"))
        result = subprocess.run(["bash", str(base / "uninstall.sh"), *arguments], cwd=base,
                                env=environment, capture_output=True, text=True, timeout=10)
        return prefix, base, result

    def test_installed_uninstall_keeps_user_configuration(self):
        prefix, base, result = self.run_installed_uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[UNINSTALL-DELETE-CHECK] PASS", result.stdout)
        self.assertEqual((base / "config/env.sh").read_text(), "# user configuration\n")
        self.assertFalse((base / "releases").exists())
        self.assertFalse(os.path.lexists(base / "current"))
        self.assertFalse(os.path.lexists(prefix / "bin/hcu-cluster-run"))

    def test_installed_uninstall_purge_removes_program_and_configuration(self):
        prefix, base, result = self.run_installed_uninstall("--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[UNINSTALL-DELETE-CHECK] PASS", result.stdout)
        self.assertFalse(base.exists())
        self.assertFalse(os.path.lexists(prefix / "bin/hcu-cluster-run"))
