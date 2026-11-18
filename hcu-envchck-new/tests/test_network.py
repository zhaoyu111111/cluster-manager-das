"""platform 调用链中的 RDMA 分类和 IB 计数器规则，使用静态证据。"""
from hcu_envcheck.ib_counters import DEFAULT_IB_COUNTER_RULES, evaluate_ib_counter_samples, parse_counter
from hcu_envcheck.rdma import classify_rdma_port, evaluate_rdma_network
from hcu_envcheck.roce_health import normalize_roce_policy
from tests.helpers import Workspace


class NetworkTests(Workspace):
    def test_counter_parse_rejects_negative_boolean_and_invalid(self):
        for value in (True, -1, "bad", None):
            with self.subTest(value=value):
                self.assertIsNone(parse_counter(value))
        self.assertEqual(parse_counter("0x10"), 16)

    def counters(self):
        return {name: 0 for name in DEFAULT_IB_COUNTER_RULES}

    def test_stable_historical_count_passes(self):
        values = self.counters()
        values["symbol_error"] = 10
        self.assertEqual(evaluate_ib_counter_samples(values, values, interval_seconds=5)["status"], "PASS")

    def test_error_growth_congestion_reset_and_missing_evidence(self):
        before = self.counters()
        cases = [(dict(before, symbol_error=1), 5, "FAIL"),
                 (dict(before, port_xmit_wait=5), 5, "WARN"),
                 ({}, 5, "UNKNOWN"), (dict(before), 0, "UNKNOWN")]
        for after, interval, expected in cases:
            with self.subTest(expected=expected, interval=interval):
                self.assertEqual(evaluate_ib_counter_samples(before, after, interval_seconds=interval)["status"], expected)
        self.assertEqual(evaluate_ib_counter_samples(dict(before, symbol_error=10), before, interval_seconds=5)["status"], "UNKNOWN")

    def test_ib_and_roce_classification_requires_protocol_evidence(self):
        self.assertEqual(classify_rdma_port({"link_layer": "InfiniBand"})["current_protocol"], "NATIVE_INFINIBAND")
        port = dict(link_layer="Ethernet", gids=[dict(gid="fe80::1", type="RoCE v2", netdev="nic0")])
        self.assertEqual(classify_rdma_port(port)["current_protocol"], "ROCE")
        port["gid_type_collection_status"] = "UNAVAILABLE"
        self.assertEqual(classify_rdma_port(port)["current_protocol"], "ETHERNET_RDMA_EVIDENCE_INCOMPLETE")

    def test_no_rdma_ports_does_not_invent_transport_success(self):
        _, checks, summary = evaluate_rdma_network({"rdma_devices": []}, required=True)
        self.assertEqual(summary["rdma_current_protocol"], "NO_RDMA_PORTS")
        self.assertFalse(summary["rdma_runtime_transport_verified"])
        self.assertTrue(any(row["status"] != "PASS" for row in checks))

    def test_invalid_expected_protocol_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_rdma_network({}, expected_protocol="imaginary")
        self.assertEqual(normalize_roce_policy(None), {})
