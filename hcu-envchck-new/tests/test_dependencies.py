"""源码快照、实际扩展指纹与 GPU 数量检查；扩展导入均为测试替身。"""
import contextlib
import hashlib
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hcu_envcheck.dependencies import export_sources, source_snapshot
from hcu_envcheck.payloads import dependency_check, gpu_check
from tests.helpers import Workspace, write_json


class DependencyTests(Workspace):
    def snapshot(self, library="DeepEP"):
        directory = self.work / "third_party" / library
        files = ({"tests/test_intranode.py": b"# unit sample\n", "LICENSE": b"unit license\n"} if library == "DeepEP"
                 else {"test/python/test_ag_kernel.py": b"# unit sample\n"})
        for name, data in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        write_json(directory / "SOURCE_MANIFEST.json", dict(library=library, commit="a" * 40,
                   files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()}))
        return directory, files

    def test_valid_offline_snapshots_and_export_without_git(self):
        for library in ("DeepEP", "flux"):
            directory, files = self.snapshot(library)
            result, metadata = source_snapshot(self.work, library)
            self.assertEqual(result, files)
            self.assertEqual(metadata["commit"], "a" * 40)
        export_sources(self.work, self.work / "export")
        self.assertTrue((self.work / "export/third_party/DeepEP/LICENSE").is_file())
        self.assertNotIn("source", json.loads((self.work / "export/third_party/flux/SOURCE_MANIFEST.json").read_text(encoding="utf-8")))

    def test_changed_hash_wrong_library_commit_and_path_escape_rejected(self):
        directory, _ = self.snapshot()
        manifest = directory / "SOURCE_MANIFEST.json"
        original = json.loads(manifest.read_text(encoding="utf-8"))
        variants = [dict(original, library="flux"), dict(original, commit="not-a-commit"),
                    dict(original, files={"../escape": "a" * 64}),
                    dict(original, files={"tests/test_intranode.py": "0" * 64})]
        for data in variants:
            with self.subTest(data=data), self.assertRaises((ValueError, OSError)):
                write_json(manifest, data)
                source_snapshot(self.work, "DeepEP")

    def test_required_test_and_license_missing_rejected(self):
        directory, _ = self.snapshot()
        write_json(directory / "SOURCE_MANIFEST.json", dict(library="DeepEP", commit="a" * 40, files={}))
        with self.assertRaises(ValueError):
            source_snapshot(self.work, "DeepEP")

    def install_stub_modules(self, library="DeepEP"):
        package = self.work / "python-packages"
        names = dependency_check.MODULES[library]
        relative = ({names[0]: "deep_ep/__init__.py", names[1]: "deep_ep/deep_ep_cpp.so"} if library == "DeepEP"
                    else {names[0]: "flux/__init__.py", names[1]: "flux_ths_pybind.so"})
        modules, digests = {}, {}
        files = {}
        for name, filename in relative.items():
            path = package / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            modules[name] = SimpleNamespace(__file__=str(path))
            digests[name] = dependency_check.fingerprint(path)
            files[filename] = digests[name]
        mapped = {}
        if library == "flux":
            path = package / "flux/lib/libflux_cuda.so"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unit flux library")
            files["flux/lib/libflux_cuda.so"] = dependency_check.fingerprint(path)
            mapped[str(path.resolve())] = {"path": str(path.resolve())}
        manifest = self.work / "dependencies.json"
        write_json(manifest, dict(schema_version=1, libraries={library: dict(source_commit="a" * 40,
                   modules=digests, files=files)}))
        return modules, mapped, manifest

    def inspect(self, library="DeepEP", mutation=None, no_manifest=False):
        modules, mapped, manifest = self.install_stub_modules(library)
        if mutation:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            mutation(data["libraries"][library])
            write_json(manifest, data)
        with patch.object(dependency_check.importlib, "import_module", side_effect=modules.__getitem__), \
                patch.object(dependency_check, "mapped_libraries", return_value=mapped):
            return dependency_check.inspect_dependency(library, {"commit": "a" * 40}, "" if no_manifest else str(manifest))

    def test_matching_deepep_and_flux_fingerprints(self):
        for library in ("DeepEP", "flux"):
            with self.subTest(library=library):
                self.assertEqual(self.inspect(library)["status"], "MATCH")

    def test_missing_manifest_is_unknown_not_pass(self):
        self.assertEqual(self.inspect(no_manifest=True)["reason_code"], "DEPENDENCY_UNKNOWN")

    def test_source_commit_binary_hash_and_manifest_path_conflicts(self):
        cases = [(lambda record: record.update(source_commit="b" * 40), "DEPENDENCY_SOURCE_MISMATCH"),
                 (lambda record: record["modules"].update({"deep_ep": "0" * 64}), "DEPENDENCY_BINARY_MISMATCH"),
                 (lambda record: record.update(files={"../escape.so": "0" * 64}), "DEPENDENCY_UNKNOWN"),
                 (lambda record: record.update(files={}), "DEPENDENCY_UNKNOWN")]
        for mutation, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.inspect(mutation=mutation)["reason_code"], reason)

    def test_import_error_is_missing_dependency(self):
        with patch.object(dependency_check.importlib, "import_module", side_effect=ImportError("missing.so")):
            result = dependency_check.inspect_dependency("DeepEP", {"commit": "a" * 40}, "")
        self.assertEqual(result["reason_code"], "DEPENDENCY_MISSING")

    def test_hip_runtime_device_count_without_loading_library(self):
        runtime = Mock()
        runtime.hipInit.return_value = 0
        def count(pointer):
            pointer._obj.value = 8
            return 0
        runtime.hipGetDeviceCount.side_effect = count
        with patch.object(gpu_check.ctypes.util, "find_library", return_value="unit.so"), \
                patch.object(gpu_check.ctypes, "CDLL", return_value=runtime):
            self.assertEqual(gpu_check.hip_device_count(), 8)
            runtime.hipInit.return_value = 1
            with self.assertRaises(RuntimeError):
                gpu_check.hip_device_count()

    def test_torch_gpu_precheck_insufficient_or_unavailable(self):
        cuda = SimpleNamespace(is_available=lambda: True, device_count=lambda: 4)
        with patch.dict(sys.modules, {"torch": SimpleNamespace(cuda=cuda)}), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(sys, "argv", ["gpu_check", "--runtime", "torch", "--count", "4"]):
                self.assertEqual(gpu_check.main(), 0)
            with patch.object(sys, "argv", ["gpu_check", "--runtime", "torch", "--count", "8"]), self.assertRaises(RuntimeError):
                gpu_check.main()
            cuda.is_available = lambda: False
            with patch.object(sys, "argv", ["gpu_check", "--runtime", "torch", "--count", "4"]), self.assertRaises(RuntimeError):
                gpu_check.main()
