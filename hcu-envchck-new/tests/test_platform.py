"""真实解析与规则计算，覆盖平台矩阵、信息分组及规模聚合。"""
import contextlib
import io
import json
import os
from unittest.mock import patch

from hcu_envcheck.environment import evaluate_environment, static_environment_view
from hcu_envcheck.payloads import platform_probe
from hcu_envcheck.reporting.consistency import (build_consistency_summary, build_platform_overview,
                                               render_platform_markdown, render_platform_terminal)
from hcu_envcheck.reporting import platform_report
from tests.helpers import Workspace, fact_row, fixture, platform_record


class PlatformTests(Workspace):
    def payload(self):
        return json.loads(fixture("platform.json"))

    def evaluate(self, payload, **options):
        profile = dict(platform_report.PLATFORM_PROFILE, **options)
        return evaluate_environment(payload, **profile)

    def overview(self, records):
        return build_platform_overview(records, build_consistency_summary(records))

    def test_rocminfo_filters_cpu_and_deduplicates_memory_pools(self):
        devices = platform_probe.parse_rocminfo(fixture("rocminfo.txt"))
        self.assertEqual(len(devices), 2)
        for device in devices:
            self.assertEqual(device, dict(model="TEST_CARD", architecture="gfx-test", compute_units=80,
                                          memory_bytes=64 * 1024**3))

    def test_rocminfo_units_marketing_name_fallback_and_missing_values(self):
        for unit, factor in (("B", 1), ("KB", 1024), ("MiB", 1024**2), ("GB", 1024**3)):
            with self.subTest(unit=unit):
                text = f"Agent 1\nName: gfx-test\nDevice Type: DCU\nCompute Unit: 12\nSegment: GLOBAL\nSize: 2 {unit}\n"
                device = platform_probe.parse_rocminfo(text)[0]
                self.assertEqual(device["model"], "gfx-test")
                self.assertEqual(device["memory_bytes"], 2 * factor)
        partial = platform_probe.parse_rocminfo("Agent 1\nName: gfx-test\nDevice Type: HCU\n")[0]
        self.assertIsNone(partial["compute_units"])
        self.assertIsNone(partial["memory_bytes"])

    def test_hardware_collection_statuses(self):
        cases = [(dict(rc=1, stdout=""), "UNAVAILABLE"), (dict(rc=0, stdout=""), "NO_DEVICES"),
                 (dict(rc=0, stdout=fixture("rocminfo.txt")), "COLLECTED"),
                 (dict(rc=0, stdout=fixture("rocminfo.txt"), output_truncated=True), "PARTIAL")]
        for command, expected in cases:
            with self.subTest(expected=expected), patch.object(platform_probe, "run", return_value=command):
                self.assertEqual(platform_probe.collect_hcu_hardware({"rocminfo": {"path": "/fake/rocminfo"}})["status"], expected)
        with patch.object(platform_probe, "run") as run:
            self.assertEqual(platform_probe.collect_hcu_hardware({})["status"], "UNAVAILABLE")
            run.assert_not_called()

    def test_versions_normalized_and_multiple_deepep_distributions_shown(self):
        payload = self.payload()
        payload["python"]["packages"]["deep_ep_shca"] = "another-ep"
        _, summary, _ = self.evaluate(payload)
        self.assertEqual(summary["transformer_engine_version"], "test-te")
        self.assertIn("deep-ep=test-ep", summary["deepep_version"])
        self.assertIn("deep-ep-shca=another-ep", summary["deepep_version"])
        self.assertEqual(summary["hcu_devices"], payload["hcu_hardware"]["devices"])

    def test_python_collector_reads_component_metadata_without_extension_imports(self):
        versions = {"torch": "test-torch", "transformer-engine": "test-te", "deep-ep-shca": "test-ep"}
        def version(name):
            if name not in versions:
                raise platform_probe.importlib.metadata.PackageNotFoundError(name)
            return versions[name]
        with patch.dict(os.environ, {"HCU_ENVCHECK_REQUIRED_PYTHON_PACKAGES": "[]"}), \
                patch.object(platform_probe.importlib.metadata, "version", side_effect=version):
            python, torch = platform_probe.collect_python()
        self.assertEqual(python["packages"], versions)
        self.assertEqual(torch["check_status"], "NOT_REQUESTED")

    def test_device_visibility_and_torch_failure_have_separate_checks(self):
        payload = self.payload()
        payload["driver"]["device_nodes"] = []
        payload["torch"]["hcu_available"] = False
        _, _, checks = self.evaluate(payload, expected_device_count=8)
        statuses = {row["check_id"]: row["status"] for row in checks}
        self.assertEqual(statuses["HCU_DEVICE_NODE"], "FAIL")
        self.assertEqual(statuses["TORCH_HCU_AVAILABLE"], "FAIL")
        self.assertEqual(statuses["TORCH_DEVICE_COUNT"], "FAIL")

    def test_missing_package_version_is_information_not_required_health_failure(self):
        payload = self.payload()
        payload["python"]["packages"] = {"torch": "test-torch"}
        _, summary, checks = self.evaluate(payload)
        self.assertIsNone(summary["transformer_engine_version"])
        self.assertIsNone(summary["deepep_version"])
        self.assertFalse(any("DEEPEP" in row["check_id"] or "TRANSFORMER" in row["check_id"] for row in checks))

    def test_platform_payload_requires_one_complete_matching_interval(self):
        token = "unit-token"
        valid = f"source banner\n[PLATFORM-BEGIN] token={token}\n{json.dumps(self.payload())}\n[PLATFORM-END] token={token} exit_code=0\n"
        self.assertIsNotNone(platform_report.parse_payload(valid, token))
        for text in (valid.replace("exit_code=0", "exit_code=1"), valid + valid,
                     valid.replace("[PLATFORM-END]", "[BROKEN-END]"), valid.replace(token, "other-token"),
                     valid.replace(json.dumps(self.payload()), "{}")):
            with self.subTest(text=text[:60]):
                self.assertIsNone(platform_report.parse_payload(text, token))

    def test_parse_failure_does_not_override_original_execution_failure(self):
        row = fact_row(stage="platform", target="node-001", code=255, reason_code="SSH_FAILED", log="missing.log")
        result, record = platform_report.parse_node((self.root, row, {"scenario": "per-node-container"}))
        self.assertEqual(result["reason_code"], "SSH_FAILED")
        self.assertEqual(record["execution_exit_code"], "255")
        self.assertIsNone(record["reachable"])

    def test_memory_and_shared_memory_differences_ignored(self):
        first, second = platform_record("node001"), platform_record("node002")
        second["environment"]["mem_total"] = "1028 kB"
        second["environment"]["dev_shm"]["total_bytes"] = 999
        summary = build_consistency_summary([first, second])
        self.assertEqual(summary["comparison_status"], "SAME_OBSERVED_CONFIGURATION")
        self.assertEqual(summary["configuration_group_count"], 1)

    def test_software_difference_does_not_change_health_groups(self):
        first, second = platform_record("node001"), platform_record("node002")
        second["environment"]["driver_version"] = "driver-2"
        second["environment"]["torch_version"] = "torch-2"
        report = self.overview([first, second])
        self.assertEqual(len(report["state_groups"]), 1)
        self.assertEqual(report["status_counts"], {"PASS": 2})
        self.assertEqual(len(report["software_groups"]), 2)
        self.assertEqual(report["consistency"]["comparison_status"], "DIFFERENT")
        self.assertIn("**driver-2**", render_platform_markdown(report))
        self.assertIn("≠ driver-2", render_platform_terminal(report))

    def test_full_status_vector_controls_grouping(self):
        records = [platform_record("node001"), platform_record("node002"), platform_record("node003")]
        records[1]["checks"][1].update(status="FAIL", message="device absent")
        records[2].update(reachable=None, execution_status="FAIL", execution_detail="SSH failed")
        report = self.overview(records)
        self.assertEqual(len(report["state_groups"]), 3)
        by_node = {group["nodes"][0]: group for group in report["state_groups"]}
        self.assertEqual(by_node["node001"]["status"], "PASS")
        self.assertEqual(by_node["node002"]["values"]["HCU_DEVICE_NODE"], "FAIL")
        self.assertEqual(set(by_node["node003"]["values"].values()), {"UNKNOWN"})

    def test_1000_nodes_merge_and_membership_complete(self):
        records = [platform_record(f"node{i:04d}") for i in range(1, 1001)]
        for record in records[996:998]:
            record["checks"][1].update(status="FAIL", message="device absent")
        for record in records[998:]:
            record.update(reachable=None, execution_status="FAIL")
            record["environment"] = {}
        records[995]["environment"]["dtk_version"] = "dtk-2"
        report = self.overview(records)
        self.assertEqual(sorted(group["node_count"] for group in report["state_groups"]), [2, 2, 996])
        self.assertEqual(len(report["hardware_groups"]), 2)
        self.assertEqual(len(report["software_groups"]), 3)
        for key in ("state_groups", "hardware_groups", "software_groups"):
            members = [node for group in report[key] for node in group["nodes"]]
            self.assertEqual(len(members), 1000)
            self.assertEqual(set(members), {record["node"] for record in records})

    def test_report_layout_single_status_matrix_and_info_column_order(self):
        record = platform_record()
        record["checks"].extend(dict(check_id=f"EXTRA_{i}", status="PASS", message="ok") for i in range(10))
        report = self.overview([record])
        markdown = render_platform_markdown(report)
        self.assertEqual(markdown.count("| 节点集合 | 节点数 |"), 1)
        self.assertIn("| 类型 | 配置组 | 节点数 | 节点集合 | 卡数/节点 |", markdown)
        self.assertIn("Transformer Engine", markdown)
        self.assertIn("EXTRA_9", markdown)
        self.assertIn("🟢通过", markdown)
        self.assertIn("PASS", render_platform_terminal(report))

    def test_missing_specs_and_versions_are_explicit(self):
        record = platform_record()
        record["environment"].update(hcu_devices=[], hcu_hardware_status="UNAVAILABLE",
                                     transformer_engine_version=None, deepep_version=None)
        markdown = render_platform_markdown(self.overview([record]))
        self.assertIn("未取得规格", markdown)
        self.assertIn("未检出版本", markdown)

    def test_runtime_rank_identity_excluded_from_static_environment(self):
        view = static_environment_view(dict(runtime_env={"RANK": "1", "OMPI_COMM_WORLD_RANK": "1", "NCCL_SOCKET_IFNAME": "nic"}))
        self.assertEqual(view["runtime_env"], {"NCCL_SOCKET_IFNAME": "nic"})

    def test_report_generation_preserves_input_execution_and_publishes_all_artifacts(self):
        from hcu_envcheck.dispatch import save_fact
        from hcu_envcheck.planning import build_plan, write_plan
        from hcu_envcheck.operations import platform
        from hcu_envcheck.reporting.tables import read_table, table_text
        from tests.helpers import fact
        request = self.request("platform")
        write_plan(self.root, request, build_plan(request, platform))
        rows = []
        for index, node in enumerate(("node001", "node002"), 1):
            target = f"node-{index:03d}"
            result = fact(target=target, node=node, stage="platform")
            result.stdout = (f"[PLATFORM-BEGIN] token={result.token}\n{json.dumps(self.payload())}\n"
                             f"[PLATFORM-END] token={result.token} exit_code=0\n")
            save_fact(self.root, result)
            rows += read_table(self.root / ".state/facts/platform" / f"{target}.tsv")
        (self.root / "results.tsv").write_text(table_text(list(rows[0]), rows), encoding="utf-8")
        original = {path: path.read_bytes() for path in (self.root / ".state/facts").rglob("*.tsv")}
        with contextlib.redirect_stdout(io.StringIO()):
            platform_report.generate(self.root)
        for name in ("platform-overview.json", "platform-nodes.json", "platform-checks.tsv", "platform-findings.tsv", "platform-consistency.md"):
            self.assertTrue((self.root / name).is_file(), name)
        self.assertTrue(all(path.read_bytes() == data for path, data in original.items()))
