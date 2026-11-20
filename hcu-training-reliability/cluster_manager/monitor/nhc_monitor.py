# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
import os
import json
import time
import threading
import dataclasses
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime, timedelta

from cluster_manager.event.event_bus import Event, EventBus, EventType
from cluster_manager.config.global_config import logger
from cluster_manager.platform.notify import Notify
import cluster_manager.config.global_config as global_config
from cluster_manager.launcher.fault_detection import FaultDetection
from cluster_manager.node_management.node_pool import NodePool
from cluster_manager.utils.file_utils import atomic_text_file, read_hostfile, write_ordered_hostfile
from cluster_manager.node_management.node_blacklist_manager import BlacklistManager, BlacklistConfig, FaultType


def _complete_probe(expected_nodes, passed_nodes, failed_nodes) -> bool:
    passed, failed = set(passed_nodes), set(failed_nodes)
    return passed.isdisjoint(failed) and (passed | failed) == set(expected_nodes)


@dataclasses.dataclass
class NodeSnapshot:
    start_time: Optional[float] = None
    stop_time: Optional[float] = None
    apply_time: Optional[datetime] = None
    release_time: Optional[float] = None
    running_nodes: List[str] = dataclasses.field(default_factory=list)
    fault_nodes: Dict[str, str] = dataclasses.field(default_factory=dict)
    fault_count: int = 0
    first_iter: Optional[int] = None
    first_iter_time: Optional[float] = None
    last_iter: Optional[int] = None
    last_iter_time: Optional[float] = None
    avg_iter_time: Optional[float] = None
    last_ckpt_iter: Optional[int] = None
    last_ckpt_iter_time: Optional[float] = None


class NHCMonitor(threading.Thread):
    """
    后台监控线程：
    1. 周期性队列检测 & 故障恢复 & 重新提交
    2. 周期性 NHC 检测 & 故障踢出 & 故障落盘
    3. 周期性硬件信息检测 & 落盘
    4. 实时更新当前活跃的 Snapshot

    线程安全设计：
    - NHCMonitor 与 NodePoolProxy 共用一把互斥锁（proxy.pool_lock，即 proxy._lock）
    - NHCMonitor.run() 整个监控逻辑块持有 pool_lock → 阻止 NodePoolProxy 所有函数运行
    - NodePoolProxy 所有公开方法持有 _lock → 阻止 NHCMonitor 中函数运行
    - 无需 _detector_lock / _file_lock / _snapshot_lock 等额外锁
    """
    # 各检测项的倍数（相对于 base_interval），修改此处即可调整周期
    QUEUE_CHECK_MULTIPLIER = 1       # 队列检测：每  1 * base_interval
    NHC_CHECK_MULTIPLIER = 3         # NHC 检测：每  3 * base_interval
    HW_CHECK_MULTIPLIER = 5          # HCU 硬件信息：每 5 * base_interval
    RECOVERY_CHECK_MULTIPLIER = 10   # 异常节点恢复：每 10 * base_interval

    def __init__(self, proxy: 'NodePoolProxy', event_bus: EventBus, detector: FaultDetection, interval: int = 60):
        super().__init__(daemon=True, name="NodeCheckManager")
        self._proxy = proxy
        self._event_bus = event_bus
        self._running = True
        self.interval = interval  # 基础周期（秒），默认 60s
        self.detector = detector
        self._cycle_count = 0

        # 读取环境变量控制HW检测开关，默认为true
        self._enable_hw_check = os.environ.get("ENABLE_HW_CHECK", "true").lower() in ("true", "1", "yes")
        # 读取环境变量控制NHC故障处理开关，默认为true
        self._enable_nhc_fault_handle = os.environ.get("ENABLE_NHC_FAULT_HANDLE", "true").lower() in ("true", "1", "yes")
        self._enable_slurm_check = os.environ.get("ENABLE_SLURM_CHECK", "true").lower() in ("true", "1", "yes")

        self.last_cross_check_time = 0
        self._node_status: Dict[str, Dict[str, Any]] = {}
        # 内存中只保留摘要信息，不存储完整详细数据
        self._hw_summary: Dict[str, Dict[str, Any]] = {}
        self.node_status_file = os.path.join(global_config.WORK_DIR, "workspace/node_status.json")
        self.hw_summary_file = os.path.join(global_config.WORK_DIR, "workspace/hw_summary.json")
        # 详细数据存储目录
        self.hw_detail_dir = os.path.join(global_config.WORK_DIR, "workspace/hw_detail")
        self._ensure_dir(self.hw_detail_dir)
        self._load_node_status()
        self._hw_summary = self._load_hw_summary()

        if self._enable_hw_check:
            logger.info("[NodeCheckManager] HW check enabled.")
        else:
            logger.info("[NodeCheckManager] HW check disabled by ENABLE_HW_CHECK environment variable.")
        if self._enable_slurm_check:
            logger.info("[NodeCheckManager] Slurm check enabled.")
        else:
            logger.info("[NodeCheckManager] Slurm check disabled by ENABLE_SLURM_CHECK environment variable.")

        if self._enable_nhc_fault_handle:
            logger.info("[NodeCheckManager] NHC fault handle enabled.")
        else:
            logger.info("[NodeCheckManager] NHC fault handle disabled by ENABLE_NHC_FAULT_HANDLE environment variable.")

    def _ensure_dir(self, path: str):
        """确保目录存在"""
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)

    def run(self):
        with self._proxy.pool_lock:
            self._clean_old_node_status()
        logger.info(
            f"[NodeCheckManager] Monitoring thread started. "
            f"base_interval={self.interval}s | "
            f"queue: every {self.QUEUE_CHECK_MULTIPLIER * self.interval}s | "
            f"nhc: every {self.NHC_CHECK_MULTIPLIER * self.interval}s | "
            f"hw_info: every {self.HW_CHECK_MULTIPLIER * self.interval}s | "
            f"recovery: every {self.RECOVERY_CHECK_MULTIPLIER * self.interval}s"
        )
        while self._running:
            try:
                for _ in range(self.interval):
                    if not self._running:
                        break
                    time.sleep(1)
                if not self._running:
                    break

                self._cycle_count += 1
                cycle = self._cycle_count

                # ===================== Step 0: 队列检测 (每 QUEUE_CHECK_MULTIPLIER 轮) =====================
                if cycle % self.QUEUE_CHECK_MULTIPLIER == 0:
                    with self._proxy.pool_lock:
                        if self._proxy._slurm_mgr:
                            queue_count = self._proxy._slurm_mgr.get_job_node_count()
                            if queue_count < 0:
                                logger.warning("[NodeCheckManager] Queue query failed, skip this cycle.")
                                continue
                            if queue_count == 0 :
                                if self._enable_slurm_check:
                                    logger.warning("[NodeCheckManager] Queue not found, releasing and resubmitting...")
                                    event = Event(type=EventType.NHC_MONITOR, payload={"type": "job_release"})
                                    self._event_bus.publish(event)
                                    self._proxy._resubmit_job()
                                    continue

                # ===================== Step 1: NHC 检测 (每 NHC_CHECK_MULTIPLIER 轮) =====================
                if cycle % self.NHC_CHECK_MULTIPLIER == 0:
                    with self._proxy.pool_lock:
                        pool = self._proxy._node_pool
                        running_nodes_filepath = pool.running_nodes_filepath
                        running_nodes = set(pool.running_nodes)

                        if running_nodes and running_nodes_filepath and (
                            pool.supernode_topology is None or self._proxy.current_snapshot
                        ):
                            self._perform_nhc_check(running_nodes_filepath, running_nodes)

                # ===================== Step 2: HCU 硬件信息检测 (每 HW_CHECK_MULTIPLIER 轮) =====================
                if cycle % self.HW_CHECK_MULTIPLIER == 0 and self._enable_hw_check:
                    with self._proxy.pool_lock:
                        running_nodes = set(self._proxy._node_pool.running_nodes)
                        if running_nodes:
                            self._perform_hw_check(list(running_nodes))

                # ===================== Step 3: 异常节点恢复 (每 RECOVERY_CHECK_MULTIPLIER 轮) =====================
                if cycle % self.RECOVERY_CHECK_MULTIPLIER == 0:
                    with self._proxy.pool_lock:
                        pool = self._proxy._node_pool
                        backup_nodes = set(pool.backup_nodes)
                        error_nodes = set(pool.abnormal_nodes)
                        abnormal_nodes_filepath = pool.abnormal_nodes_filepath

                        # An empty abnormal-node hostfile is valid when the
                        # cluster is healthy.  Do not pass it to clush: clush
                        # treats an empty target set as an error (exit code 2,
                        # "No node to run on").
                        if error_nodes and (
                            not backup_nodes or len(error_nodes) >= len(backup_nodes)
                        ):
                            passed, _, probe_ok = self.detector.run_nhc(abnormal_nodes_filepath)
                            if not probe_ok:
                                logger.warning("[NodeCheckManager] recovery probe failed, skip abnormal-node recovery this cycle.")
                            elif len(passed) > 0:
                                pool.add_normal_nodes(passed)

                # ===================== Step 4: 检查附加节点 (每轮都执行，开销极小) =====================
                try:
                    with self._proxy.pool_lock:
                        additional_nodes = self._proxy._try_load_additional_nodes()
                        if additional_nodes:
                            logger.info(f'>= additional_nodes:{additional_nodes}')
                except Exception as e:
                    logger.exception(f'>= 报错{e}')

            except Exception as e:
                logger.exception(f"[NodeCheckManager] Monitor loop error: {e}")

    def _perform_nhc_check(self, running_filepath, running_nodes: set):
        """执行 NHC 检测，踢出故障节点并更新 snapshot（调用方必须已持有 pool_lock）"""
        if not running_filepath:
            return

        passed_nodes, failed_nodes, probe_ok = self.detector.run_nhc(running_filepath)
        if not probe_ok or not _complete_probe(running_nodes, passed_nodes, failed_nodes):
            logger.warning("[NodeCheckManager] NHC probe command failed, skip health-status update this cycle.")
            return

        logger.info(f"[NodeCheckManager] NHC check completed. Passed: {len(passed_nodes)}, Failed: {len(failed_nodes)}")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._update_node_status(passed_nodes, "normal", now)
        self._update_node_status(failed_nodes, "abnormal", now)

        if failed_nodes:
            try:
                failed_nodes_reason = self.detector.run_nhc_nodes(failed_nodes)
            except Exception:
                logger.exception("[NodeCheckManager] Could not collect NHC fault details.")
                failed_nodes_reason = {}
            if self._enable_nhc_fault_handle:
                self._handle_faults(failed_nodes, running_nodes, failed_nodes_reason)
            else:
                logger.info(f"[NodeCheckManager] NHC fault handle disabled by ENABLE_NHC_FAULT_HANDLE environment variable. Skipping _handle_faults for failed nodes: {failed_nodes}")
            self._notify_hcu_xid_faults(failed_nodes_reason)

    def _notify_hcu_xid_faults(self, failed_nodes_reason: Optional[Dict[str, str]]):
        """Send Feishu alerts when NHC fault reason contains hcu_xid."""
        if not failed_nodes_reason or not isinstance(failed_nodes_reason, dict):
            return

        xid_nodes = []
        xid_details = []
        for node, reason in failed_nodes_reason.items():
            reason_text = str(reason or "")
            if "hcu_xid" not in reason_text.lower():
                continue
            xid_nodes.append(node)
            for line in reason_text.splitlines():
                if "hcu_xid" in line.lower():
                    xid_details.append(f"{node}: {line.strip()}")
                    break

        if not xid_nodes:
            return

        message_lines = [
            "[NHC巡检告警] 检测到节点 hcu_xid 故障",
            f"故障节点: {', '.join(xid_nodes)}",
        ]
        if xid_details:
            message_lines.append("故障详情:")
            message_lines.extend(xid_details[:])

        message = "\n".join(message_lines)
        logger.warning(message)
        self._proxy._notify.send_feishu_alert(message)

    def _perform_hw_check(self, running_nodes: List[str]):
        """
        检测硬件信息并落盘（调用方必须已持有 pool_lock）

        优化策略：
        1. 详细数据按节点名分目录存储，每个节点一个文件夹
        2. 内存中只保留摘要统计和异常节点信息
        3. 提供查询接口按需读取详细数据
        """
        running_nodes_file = self._proxy._node_pool.running_nodes_filepath
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        timestamp_filename = now.replace(':', '-').replace(' ', '_')

        # 获取原始数据
        mem_info = self.detector.get_mem_info(running_nodes_file)
        hcu_info = self.detector.get_hcu_info(running_nodes_file)
        nodes_info = self.detector.get_nodes_info(running_nodes_file)

        # 生成摘要信息
        summary = self._generate_hw_summary(mem_info, hcu_info, nodes_info, now)

        # 按节点存储详细数据
        self._save_hw_detail_by_node(mem_info, hcu_info, nodes_info, timestamp_filename, now)

        # 内存中只保留摘要
        self._hw_summary[now] = summary
        self._append_to_json(self.hw_summary_file, {now: summary})

        logger.info(f"[NodeCheckManager] HW check completed. Summary: nodes={summary['summary']['total_nodes']}, "
                    f"hcu_alerts={len(summary['alerts']['hcu_alerts'])}, mem_alerts={len(summary['alerts']['mem_alerts'])}")

    def _save_hw_detail_by_node(self, mem_info, hcu_info, nodes_info, timestamp_filename: str, timestamp: str):
        """
        按节点存储详细数据，每个节点一个文件夹

        文件结构：
        hw_detail/
        ├── node1/
        │   ├── 2024-01-01_12-00-00.json
        │   └── ...
        ├── node2/
        │   ├── 2024-01-01_12-00-00.json
        │   └── ...
        """
        # 收集所有节点名
        all_nodes = set()

        if isinstance(hcu_info, dict):
            all_nodes.update(hcu_info.keys())

        if isinstance(mem_info, list):
            for node_data in mem_info:
                if isinstance(node_data, dict) and "node" in node_data:
                    all_nodes.add(node_data["node"])

        if isinstance(nodes_info, list):
            for node_data in nodes_info:
                if isinstance(node_data, dict) and "node" in node_data:
                    all_nodes.add(node_data["node"])

        # 为每个节点存储数据
        for node_name in all_nodes:
            node_dir = os.path.join(self.hw_detail_dir, node_name)
            self._ensure_dir(node_dir)

            node_data = {
                "timestamp": timestamp,
                "node": node_name,
                "hcu": {},
                "mem": {},
                "node_info": {}
            }

            # 提取该节点的HCU信息
            if isinstance(hcu_info, dict) and node_name in hcu_info:
                node_data["hcu"] = hcu_info[node_name]

            # 提取该节点的内存信息
            if isinstance(mem_info, list):
                for mem_data in mem_info:
                    if isinstance(mem_data, dict) and mem_data.get("node") == node_name:
                        node_data["mem"] = {
                            "Mem": mem_data.get("Mem", {}),
                            "Swap": mem_data.get("Swap", {})
                        }
                        break

            # 提取该节点的scontrol信息
            if isinstance(nodes_info, list):
                for node_data_item in nodes_info:
                    if isinstance(node_data_item, dict) and node_data_item.get("node") == node_name:
                        node_data["node_info"] = node_data_item
                        break

            # 写入节点数据文件
            node_file = os.path.join(node_dir, f"{timestamp_filename}.json")
            self._write_json_file(node_file, node_data)

    def _generate_hw_summary(self, mem_info, hcu_info, nodes_info, timestamp: str) -> Dict[str, Any]:
        """
        生成硬件信息摘要，大幅减少内存占用

        返回结构：
        {
            "timestamp": "2024-01-01 12:00:00",
            "summary": {
                "total_nodes": 1000,
                "total_hcu_cards": 8000,
                "avg_hcu_vram_usage": 75.5,
                "avg_hcu_power": 250.3,
                "avg_mem_usage": 60.2,
                "avg_swap_usage": 5.1
            },
            "alerts": {
                "hcu_alerts": [...],  # 只包含异常HCU
                "mem_alerts": [...],  # 只包含内存异常节点
                "node_alerts": [...]  # 只包含异常节点信息
            },
            "detail_file": "2024-01-01_12-00-00.json"  # 详细数据文件名
        }
        """
        summary = {
            "timestamp": timestamp,
            "summary": {
                "total_nodes": 0,
                "total_hcu_cards": 0,
                "avg_hcu_vram_usage": 0.0,
                "avg_hcu_power": 0.0,
                "avg_hcu_use": 0.0,
                "avg_mem_usage": 0.0,
                "avg_swap_usage": 0.0
            },
            "alerts": {
                "hcu_alerts": [],
                "mem_alerts": [],
                "node_alerts": []
            },
            "detail_file": f"{timestamp.replace(':', '-').replace(' ', '_')}.json"
        }

        # HCU 信息统计
        hcu_vram_list = []
        hcu_power_list = []
        hcu_use_list = []
        hcu_alerts = []

        if isinstance(hcu_info, dict):
            for node_name, cards in hcu_info.items():
                summary["summary"]["total_nodes"] += 1
                if isinstance(cards, dict):
                    for card_key, card_metrics in cards.items():
                        summary["summary"]["total_hcu_cards"] += 1
                        vram = card_metrics.get("vram", 0)
                        power = card_metrics.get("power", 0)
                        use = card_metrics.get("use", 0)
                        available = card_metrics.get("available", 1)

                        hcu_vram_list.append(vram)
                        hcu_power_list.append(power)
                        hcu_use_list.append(use)

                        # 检测HCU异常（显存>90%或利用率异常）
                        vram_usage = vram / available if available > 0 else 0
                        if vram_usage > 0.9 or use > 95:
                            hcu_alerts.append({
                                "node": node_name,
                                "card": card_key,
                                "vram_usage_pct": round(vram_usage * 100, 2),
                                "use_pct": use,
                                "power": power
                            })

        if hcu_vram_list:
            summary["summary"]["avg_hcu_vram_usage"] = round(sum(hcu_vram_list) / len(hcu_vram_list), 2)
        if hcu_power_list:
            summary["summary"]["avg_hcu_power"] = round(sum(hcu_power_list) / len(hcu_power_list), 2)
        if hcu_use_list:
            summary["summary"]["avg_hcu_use"] = round(sum(hcu_use_list) / len(hcu_use_list), 2)

        summary["alerts"]["hcu_alerts"] = hcu_alerts

        # 内存信息统计
        mem_usage_list = []
        swap_usage_list = []
        mem_alerts = []

        if isinstance(mem_info, list):
            for node_data in mem_info:
                mem = node_data.get("Mem", {})
                swap = node_data.get("Swap", {})
                node_name = node_data.get("node", "unknown")

                mem_total = mem.get("total", 0)
                mem_used = mem.get("used", 0)
                swap_total = swap.get("total", 0)
                swap_used = swap.get("used", 0)

                if mem_total > 0:
                    mem_usage_pct = mem_used / mem_total * 100
                    mem_usage_list.append(mem_usage_pct)

                    # 内存使用超过80%告警
                    if mem_usage_pct > 80:
                        mem_alerts.append({
                            "node": node_name,
                            "type": "mem_high",
                            "usage_pct": round(mem_usage_pct, 2),
                            "used_mb": mem_used,
                            "total_mb": mem_total
                        })

                if swap_total > 0:
                    swap_usage_pct = swap_used / swap_total * 100
                    swap_usage_list.append(swap_usage_pct)

                    # Swap使用超过50%告警
                    if swap_usage_pct > 50:
                        mem_alerts.append({
                            "node": node_name,
                            "type": "swap_high",
                            "usage_pct": round(swap_usage_pct, 2),
                            "used_mb": swap_used,
                            "total_mb": swap_total
                        })

        if mem_usage_list:
            summary["summary"]["avg_mem_usage"] = round(sum(mem_usage_list) / len(mem_usage_list), 2)
        if swap_usage_list:
            summary["summary"]["avg_swap_usage"] = round(sum(swap_usage_list) / len(swap_usage_list), 2)

        summary["alerts"]["mem_alerts"] = mem_alerts

        # 节点信息统计（只记录有异常的节点）
        node_alerts = []
        if isinstance(nodes_info, list):
            for node_data in nodes_info:
                reason = node_data.get("reason", "")
                node_name = node_data.get("node", "unknown")
                # 只记录有异常原因的节点
                if reason and "没有异常输出" not in reason:
                    node_alerts.append({
                        "node": node_name,
                        "reason": reason
                    })

        summary["alerts"]["node_alerts"] = node_alerts

        return summary

    def get_hw_summary(self, timestamp: str = None) -> Dict[str, Any]:
        """
        获取硬件信息摘要

        Args:
            timestamp: 指定时间戳，为None则返回最新的摘要

        Returns:
            硬件信息摘要字典
        """
        if timestamp:
            return self._hw_summary.get(timestamp, {})
        elif self._hw_summary:
            # 返回最新的摘要
            latest_ts = max(self._hw_summary.keys())
            return self._hw_summary[latest_ts]
        return {}


    def _perform_backup_cross_check(self, all_nodes: List[str]):
        """跨节点交叉检查（自身持有 pool_lock，阻止 NodePoolProxy）"""
        with self._proxy.pool_lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if now - self.last_cross_check_time < 600:
                return
            logger.info("[HealthManager] No backup nodes! Triggering cross_check.")
            self.last_cross_check_time = now
            normal_nodes, abnormal_nodes = self.detector.cross_check(all_nodes)
            self._update_node_status(normal_nodes, "normal", now, source="cross_check")
            self._update_node_status(abnormal_nodes, "abnormal", now, source="cross_check")

    def _update_node_status(self, nodes: List[str], status: str, timestamp: str, source: str = "periodic_nhc"):
        if not nodes:
            return
        node_status = {}
        for node in nodes:
            node_status[node] = {
                "status": status,
                "timestamp": timestamp,
                "source": source
            }
        self._node_status[str(timestamp)] = node_status
        self._append_to_json(self.node_status_file, {str(timestamp): node_status})

    def _handle_faults(self, failed_nodes: List[str], running_set: set,
                       failed_nodes_reason: Optional[Dict[str, str]] = None):
        """Quarantine confirmed running-node faults before asking the controller to stop."""
        fault_list = [node for node in failed_nodes if node in running_set]
        if not fault_list:
            return

        launched = list(self._proxy._node_pool.running_nodes)
        changed, _, _, _ = self._proxy._node_pool.add_abnormal_nodes(fault_list)
        if not changed:
            return
        logger.warning(f"[NodeCheckManager] Detected FAULT in running nodes: {fault_list}")

        current_snap = self._proxy.current_snapshot
        if current_snap:
            current_snap.running_nodes = launched
            reasons = failed_nodes_reason or {}
            current_snap.fault_nodes.update({node: reasons.get(node, "NHC failed") for node in fault_list})
            current_snap.fault_count += 1
            self._proxy._persist_current_snapshot()
        event = Event(type=EventType.NHC_MONITOR, payload={"type": "fault", "abnormal_nodes": fault_list})
        self._event_bus.publish(event)

    def _append_to_json(self, file_path: str, data: Any):
        """追加写入 JSON 文件（JSONL 格式，调用方必须已持有 pool_lock）"""
        try:
            with open(file_path, 'a') as f:
                f.write(json.dumps(data, default=str) + '\n')
        except Exception as e:
            logger.exception(f"[NodeCheckManager] Append file {file_path} failed: {e}")


    def _write_json_file(self, file_path: str, data: Any):
        try:
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=4, default=str)
        except Exception as e:
            logger.exception(f"[NodeCheckManager] Save file {file_path} failed: {e}")

    def _parse_ts_key(self, ts_key: Any) -> Optional[datetime]:
        """Parse timestamp key from datetime string or epoch-like value."""
        if ts_key is None:
            return None
        if isinstance(ts_key, datetime):
            return ts_key
        if isinstance(ts_key, (int, float)):
            try:
                return datetime.fromtimestamp(float(ts_key))
            except (TypeError, ValueError, OSError):
                return None
        if isinstance(ts_key, str):
            ts_str = ts_key.strip()
            if not ts_str:
                return None
            try:
                return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
            try:
                return datetime.fromtimestamp(float(ts_str))
            except (TypeError, ValueError, OSError):
                return None
        return None

    def _clean_old_node_status(self):
        """清理 node_status_file 里一天前的日志（调用方必须已持有 pool_lock）"""
        cutoff_dt = datetime.now() - timedelta(days=1)
        keys_to_remove = []
        for k in self._node_status:
            ts_dt = self._parse_ts_key(k)
            if ts_dt is None:
                logger.debug(f"[NodeCheckManager] Skip invalid node status timestamp key in memory: {k}")
                continue
            if ts_dt < cutoff_dt:
                keys_to_remove.append(k)
        for k in keys_to_remove:
            del self._node_status[k]

        if os.path.exists(self.node_status_file):
            valid_lines = []
            try:
                with open(self.node_status_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            for ts_key in data.keys():
                                ts_dt = self._parse_ts_key(ts_key)
                                if ts_dt is None:
                                    logger.debug(f"[NodeCheckManager] Skip invalid node status timestamp key in file: {ts_key}")
                                    continue
                                if ts_dt >= cutoff_dt:
                                    valid_lines.append(line)
                                    break
                        except json.JSONDecodeError:
                            continue
                with open(self.node_status_file, 'w') as f:
                    for line in valid_lines:
                        f.write(line + '\n')
            except Exception as e:
                logger.exception(f"[NodeCheckManager] Clean old node status failed: {e}")

    def _load_node_status(self):
        """从磁盘加载节点状态（仅在 __init__ 中调用，无需加锁）"""
        if not os.path.exists(self.node_status_file):
            return {}
        try:
            node_status = {}
            with open(self.node_status_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        node_status.update(data)
                    except json.JSONDecodeError:
                        continue
            return node_status
        except Exception as e:
            logger.exception(f"[NodeCheckManager] Load node status failed: {e}")
            return {}

    def _load_hw_summary(self):
        """从磁盘加载硬件摘要信息（仅在 __init__ 中调用，无需加锁）"""
        if not os.path.exists(self.hw_summary_file):
            return {}
        try:
            hw_summary = {}
            with open(self.hw_summary_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        hw_summary.update(data)
                    except json.JSONDecodeError:
                        continue
            return hw_summary
        except Exception as e:
            logger.error(f"[NodeCheckManager] Load hw summary failed: {e}")
            return {}

    def stop(self):
        self._running = False


class NodePoolProxy:
    """
    唯一对外出口：
    1. 代理 apply 与 release 接口，并在期间维护完整的 Snapshot 生命周期
    2. 透传原 NodePool 的所有其他接口

    线程安全设计：
    - self._lock (RLock) 是唯一的互斥锁，NHCMonitor 通过 pool_lock 属性共享同一把锁
    - 所有公开方法持有 self._lock → 阻止 NHCMonitor 监控函数运行
    - NHCMonitor.run() 监控逻辑持有 pool_lock → 阻止本类公开方法运行
    - snapshot 读写不再需要额外锁，统一由 _lock 保护
    """

    def __init__(self, node_pool: NodePool, event_bus: EventBus, slurm_mgr=None):
        self._node_pool = node_pool
        self._slurm_mgr = slurm_mgr
        self._lock = threading.RLock()
        self.detector = FaultDetection()
        self.detector.require_complete_nhc = node_pool.supernode_topology is not None
        # self.nodeblack = NodeBlacklist()
        self._event_bus = event_bus
        self._notify = Notify()

        # 记录当前所需节点数，供 NHCMonitor 判断是否需要做队列检测
        self._required_num: int = 0

        self.snapshots_file = os.path.join(global_config.WORK_DIR, "workspace/node_snapshots.json")
        self.current_snapshot_file = os.path.join(global_config.WORK_DIR, "workspace/current_snapshot.json.tmp")
        self.snapshots: Dict[str, NodeSnapshot] = {}
        self.current_snapshot: Optional[NodeSnapshot] = None
        self.last_snapshot: Optional[NodeSnapshot] = None

        self._load_snapshots()
        self._recover_current_snapshot()
        interval = global_config.INTERVAL_MONITOR
        self._check_manager = NHCMonitor(self, event_bus, self.detector, interval)

        bl_cfg = BlacklistConfig(
            persistence_path=global_config.BLACKLIST_PERSISTENCE_PATH,
            persistence_backup_path=global_config.BLACKLIST_PERSISTENCE_BACKUP_PATH,
        )

        self.bl_mgr = BlacklistManager.get_instance(bl_cfg)
        self.bl_mgr.start() # 启动异步落盘后台线程



    @property
    def pool_lock(self):
        """NHCMonitor 通过此属性获取唯一的互斥锁，保证跨线程安全"""
        return self._lock

    def _reset_slurm_pool(self, nodes):
        isolated = []
        if self._node_pool.supernode_topology is not None:
            allocated = set(nodes)
            isolated = [n for n in self._node_pool.abnormal_nodes if n in allocated]
        self._node_pool.reset_node_pool(nodes, isolated)

    # =========================================================================

    # =========================================================================
    # 提取：apply 中两处完全相同的重试提交循环
    #
    # 前置条件：调用方必须已持有 self._lock（RLock level >= 1）。
    # 内部通过 release/acquire 临时释放锁以允许其他线程在等待期间操作。
    # =========================================================================
    def _resubmit_job(self, submit_retry_interval: int = 180,
                      submit_max_retries: int = 0) -> bool:
        """
        尝试重新提交作业，封装 apply_node_num_resources 中两处完全相同的重试循环。

        前置条件：调用方必须已持有 self._lock（RLock）。
        内部会在等待期间临时释放锁（release 一次 RLock 层级），允许其他线程操作。

        返回：
            True  - 作业提交成功或已在队列中恢复
            False - 重试耗尽，彻底失败
        """
        if self._slurm_mgr is None:
            logger.error("[Proxy] Cannot resubmit a job when CLUSTER_SCHEDULE=NONE.")
            return False

        submit_retry_count = 0
        while True:
            submitted = self._slurm_mgr.submit_and_get_nodes()
            if submitted:
                logger.info("[Proxy] Job resubmitted successfully.")
                submit_retry_count = 0
                self._reset_slurm_pool(read_hostfile(self._slurm_mgr.hostfile))
                return True

            submit_retry_count += 1
            if submit_max_retries > 0 and submit_retry_count >= submit_max_retries:
                logger.error(
                    f"[Proxy] Job resubmit failed after {submit_retry_count} retries. Giving up."
                )
                return False

            logger.warning(
                f"[Proxy] Job resubmit failed, sleep {submit_retry_interval}s "
                f"and retry (attempt {submit_retry_count})..."
            )
            # 临时释放锁：RLock count 从 1→0，其他线程可操作；sleep 后重新获取 0→1
            self._lock.release()
            try:
                time.sleep(submit_retry_interval)
            finally:
                self._lock.acquire()

            query_ok, nodes = self._slurm_mgr.get_job_nodes(verbose=False)
            count = len(nodes) if query_ok else -1
            if count > 0:
                logger.info(f"[Proxy] Job reappeared with {count} nodes after sleep.")
                self._reset_slurm_pool(nodes)
                return True

            if count < 0:
                logger.warning("[Proxy] Queue query failed after sleep, keep retrying resubmit flow.")

    # =========================================================================
    # 原有方法
    # =========================================================================

    def _try_load_additional_nodes(self, allocated_nodes=None):
        """
        Probe requested additions under self._lock. In Slurm topology mode,
        keep hosts pending until they belong to the current allocation.
        """
        additional_nodes_file = os.path.join(global_config.WORK_DIR, 'workspace/.node_pool/additional_nodes.txt')
        succ_additional_nodes_file = os.path.join(global_config.WORK_DIR, 'workspace/.node_pool/succ_additional_nodes.txt')

        if not os.path.exists(additional_nodes_file):
            return []

        nodes = []
        with open(additional_nodes_file, 'r') as f:
            for line in f:
                node = line.strip()
                if node:
                    nodes.append(node)

        if not nodes:
            return []

        if self._node_pool.supernode_topology is not None and self._slurm_mgr is not None:
            if allocated_nodes is None:
                query_ok, allocated_nodes = self._slurm_mgr.get_job_nodes(verbose=False)
                if not query_ok:
                    logger.warning("[Proxy] Cannot verify the Slurm allocation for additional nodes.")
                    return []
            allowed_nodes = set(allocated_nodes)
            pending_nodes = list(dict.fromkeys(node for node in nodes if node not in allowed_nodes))
            if pending_nodes:
                logger.warning(f"[Proxy] Additional nodes awaiting Slurm allocation: {pending_nodes}")
            nodes = [node for node in nodes if node in allowed_nodes]

        unique_nodes = list(dict.fromkeys(nodes))

        if not unique_nodes:
            return []
        passed_set = set(self.detector._run_nodes_nhc(unique_nodes, timeout=120) or [])
        passed = [node for node in unique_nodes if node in passed_set]
        if not passed:
            logger.warning(f"[Proxy] All {len(unique_nodes)} additional nodes failed NHC check.")
        actual_added = self._node_pool.add_total_nodes(passed) if passed else []

        # 将成功添加的节点追加写入 succ_additional_nodes.txt
        if actual_added:
            try:
                # 先读取已有的成功节点，用于去重
                existing_succ_nodes = set()
                if os.path.exists(succ_additional_nodes_file):
                    with open(succ_additional_nodes_file, 'r') as f:
                        for line in f:
                            node = line.strip()
                            if node:
                                existing_succ_nodes.add(node)

                # 追加新节点（去重）
                new_nodes_to_write = [n for n in actual_added if n not in existing_succ_nodes]
                if new_nodes_to_write:
                    with open(succ_additional_nodes_file, 'a') as f:
                        for node in new_nodes_to_write:
                            f.write(node + '\n')
                    logger.info(f"[Proxy] Appended {len(new_nodes_to_write)} nodes to succ_additional_nodes.txt")
            except Exception as e:
                logger.error(f"[Proxy] Failed to update succ_additional_nodes.txt: {e}")

        # Operators can add names while NHC runs. Acknowledge only names we processed.
        processed = set(unique_nodes)
        pending_nodes = [node for node in read_hostfile(additional_nodes_file, preserve_order=True)
                         if node not in processed]
        write_ordered_hostfile(additional_nodes_file, pending_nodes)
        return actual_added or []

    def _persist_current_snapshot(self):
        """将 current_snapshot 实时保存到磁盘（调用方必须已持有 self._lock）"""
        if self.current_snapshot:
            try:
                data = dataclasses.asdict(self.current_snapshot)
                with atomic_text_file(self.current_snapshot_file) as f:
                    json.dump(data, f, indent=4, default=str)
            except Exception as e:
                logger.error(f"[Proxy] Failed to persist current snapshot: {e}")

    def _load_snapshots(self):
        """从磁盘加载历史快照（按行解析JSONL，仅在 __init__ 中调用，无需加锁）"""
        if not os.path.exists(self.snapshots_file):
            return

        try:
            with open(self.snapshots_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        for release_time_str, snap_dict in data.items():
                            snapshot = NodeSnapshot(**snap_dict)
                            self.snapshots[release_time_str] = snapshot
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.error(f"[Proxy] Failed to parse snapshot line: {line}, error: {e}")
                        continue

            if self.snapshots:
                def parse_time(time_str):
                    return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                latest_time_str = max(self.snapshots.keys(), key=parse_time)
                self.last_snapshot = self.snapshots[latest_time_str]

        except Exception as e:
            logger.error(f"[Proxy] Load snapshots failed: {e}")

    def _recover_current_snapshot(self):
        """程序启动时，检查是否存在未保存完成的 current_snapshot（仅在 __init__ 中调用，无需加锁）"""
        if os.path.exists(self.current_snapshot_file):
            try:
                with open(self.current_snapshot_file, 'r') as f:
                    data = json.load(f)
                recovered_snapshot = NodeSnapshot(**data)
                logger.warning(f"[Proxy] Recovered unfinished snapshot from disk (applied at: {recovered_snapshot.apply_time}).")
                self.current_snapshot = recovered_snapshot
            except Exception as e:
                logger.error(f"[Proxy] Failed to recover current snapshot: {e}")
                try:
                    os.remove(self.current_snapshot_file)
                except:
                    pass


    def _recover_allocation_capacity(self, required, deficit, allocated_nodes):
        """Keep the existing Slurm supplement/recovery loop; retry placement after it."""
        count = (len(allocated_nodes) if allocated_nodes is not None
                 else self._slurm_mgr.get_job_node_count())
        if count < 0:
            logger.warning("[Proxy] Slurm query failed; will retry allocation.")
            return True
        if count == 0:
            self.release_runing_nodes()
            return self._resubmit_job()

        additional = self._try_load_additional_nodes(allocated_nodes)
        recovered = []
        abnormal = self._node_pool.abnormal_nodes
        if count >= required and len(additional) < deficit and abnormal:
            passed, failed, probe_ok = self.detector.run_nhc(self._node_pool.abnormal_nodes_filepath)
            if probe_ok and _complete_probe(abnormal, passed, failed) and passed:
                _, _, recovered = self._node_pool.add_normal_nodes(passed)
        if len(additional) + len(recovered) < deficit:
            self._notify.send_feishu_alert(
                f"[节点补充] 作业需要{required}台，本轮补充{len(additional)}台、恢复{len(recovered)}台，"
                "等待可用节点后重新分配。"
            )
        return True

    def apply_node_num_resources(self, required_num: int, slots_per_node: int) -> Tuple[Any, str]:
        """Allocate a full launch layout and publish it only after every rank host passes NHC."""
        pool = self._node_pool
        topology_enabled = pool.supernode_topology is not None
        candidate = None
        retry = False
        while True:
            if retry:
                time.sleep(60)
            retry = True
            with self._lock:
                self._required_num = required_num
                allocated = None
                if topology_enabled and self._slurm_mgr is not None:
                    query_ok, allocated = self._slurm_mgr.get_job_nodes(verbose=False)
                    if not query_ok:
                        logger.warning("[Proxy] Cannot verify the Slurm allocation; retrying.")
                        continue
                    if not allocated:
                        self.release_runing_nodes()
                        if not self._resubmit_job():
                            return None, None
                        candidate = None
                        continue
                    allowed = set(allocated)
                    total = pool.total_nodes
                    if not allowed.issuperset(total):
                        self._reset_slurm_pool([node for node in total if node in allowed])
                        candidate = None

                running = list(pool.running_nodes)
                if candidate is None or running != candidate:
                    # The topology planner needs the entire ordered group on every retry.
                    # Legacy pools append newly allocated nodes to any running ones.
                    request = required_num if topology_enabled else required_num - len(running)
                    if request > 0:
                        result = pool.apply_node_num_resources(request, slots_per_node)
                        if result and result[1]:
                            slots_file = result[1]
                            running = list(pool.running_nodes)
                        else:
                            candidate = None
                            if self._slurm_mgr is None:
                                logger.error(f"[Proxy] No complete bare-metal layout for {required_num} nodes.")
                                return None, None
                            deficit = max(1, required_num - len(running))
                            if not self._recover_allocation_capacity(required_num, deficit, allocated):
                                return None, None
                            continue
                    elif request == 0:
                        slots_file = pool.slots_filepath
                    if len(running) != required_num or len(set(running)) != required_num or not running:
                        logger.error("[Proxy] Node pool returned an inconsistent launch layout.")
                        return None, None
                    candidate = running.copy()

                if topology_enabled:
                    # Initial placement checks every local standby as well as the active hosts.
                    # Recovery is allowed to use the remaining capacity of the selected Pods.
                    probe_nodes, probe_file = pool.nhc_probe_layout()
                    if not set(candidate).issubset(probe_nodes):
                        logger.error("[Proxy] Incomplete node pool NHC hostfile for the planned launch.")
                        return None, None
                    if not set(probe_nodes).isdisjoint(pool.abnormal_nodes):
                        pool.release_runing_nodes()
                        candidate = None
                        continue
                else:
                    probe_nodes, probe_file = candidate, pool.running_nodes_filepath
                passed, failed, probe_ok = self.detector.run_nhc(probe_file)
                if not probe_ok or not _complete_probe(probe_nodes, passed, failed):
                    logger.warning("[Proxy] Incomplete NHC response; will probe the same layout again.")
                    continue
                if failed:
                    changed, cleared, _, _ = pool.add_abnormal_nodes(failed)
                    if changed:
                        if topology_enabled and not cleared:
                            # A standby-only fault also invalidates the initial reservation.
                            pool.release_runing_nodes()
                        for node in failed:
                            self.bl_mgr.report_fault(node, FaultType.HCU, description="Pre-launch NHC failed")
                        candidate = None
                        retry = False
                    continue

                # NHC reports by health/result group. NodePool's order defines ranks.
                if topology_enabled:
                    slots_file = pool.slots_filepath
                write_ordered_hostfile(slots_file, candidate, slots_per_node)
                self.current_snapshot = NodeSnapshot(
                    apply_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    running_nodes=candidate.copy(),
                )
                self._persist_current_snapshot()
                logger.info(f"[Proxy] Allocated {required_num} healthy nodes with {slots_per_node} slots each.")
                return candidate[0], slots_file

    def restore_legacy_layout(self, slots):
        """Align an older NHC snapshot with the slot order of the live training job."""
        with self._lock:
            self._node_pool.restore_legacy_layout(slots)
            running = list(self._node_pool.running_nodes)
            if self.current_snapshot and running and set(self.current_snapshot.running_nodes) == set(running):
                self.current_snapshot.running_nodes = running
                self._persist_current_snapshot()

    def confirm_running_layout(self, slots):
        """Record the recovery baseline only after the launcher accepts the layout."""
        with self._lock:
            return self._node_pool.confirm_running_layout(slots)

    def release_runing_nodes(self):
        """释放当前运行节点（持有 self._lock → 阻止 NHCMonitor 运行）"""
        with self._lock:
            nodes_to_release = list(self._node_pool.running_nodes)
            logger.info(f"[Proxy] Releasing nodes: {nodes_to_release}")

            ret = self._node_pool.release_runing_nodes()

            if self.current_snapshot:
                self.current_snapshot.release_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if nodes_to_release:
                    self.current_snapshot.running_nodes = nodes_to_release
                self.last_snapshot = self.current_snapshot

                release_time_str = str(self.current_snapshot.release_time)
                self.snapshots[release_time_str] = self.current_snapshot

                self._save_snapshots()
                self.current_snapshot = None
                if os.path.exists(self.current_snapshot_file):
                    try:
                        os.remove(self.current_snapshot_file)
                    except Exception as e:
                        logger.error(f"[Proxy] Failed to remove current snapshot file: {e}")

            # 训练结束，清零标记，防止 NHCMonitor 对已释放的池做队列检测
            self._required_num = 0

            return ret

    def normal_nodes_file(self):
        with self._lock:
            return self._node_pool.normal_nodes_file

    def abnormal_nodes_file(self):
        with self._lock:
            return self._node_pool.abnormal_nodes_file

    def abnormal_nodes(self):
        with self._lock:
            return self._node_pool.abnormal_nodes

    def normal_nodes(self):
        with self._lock:
            return self._node_pool.normal_nodes

    def backup_nodes(self):
        with self._lock:
            return self._node_pool.backup_nodes

    def _save_snapshots(self):
        """将快照持久化到磁盘（追加写，调用方必须已持有 self._lock）"""
        try:
            if self.last_snapshot:
                data = {str(self.last_snapshot.release_time): dataclasses.asdict(self.last_snapshot)}
                with open(self.snapshots_file, 'a') as f:
                    f.write(json.dumps(data, default=str) + '\n')
                logger.info(f"[Proxy] Snapshot appended to disk successfully. Total: {len(self.snapshots)}")
        except Exception as e:
            logger.exception(f"[Proxy] Save snapshots failed: {e}")

    def get_snapshots_by_timestamp(self, query_time: float) -> List[NodeSnapshot]:
        """传入时间点，返回往前倒推半小时到查询时间点之间的snapshots"""
        start_time = query_time - 1800
        result = []
        with self._lock:
            for release_time_str, snapshot in self.snapshots.items():
                release_time = float(release_time_str)
                if start_time <= release_time <= query_time:
                    result.append(snapshot)
        return result


    def get_current_snapshot(self):
        with self._lock:
            return self.current_snapshot if self.current_snapshot else None

    def get_snapshots(self):
        with self._lock:
            return self.snapshots

    def get_last_snapshot(self):
        with self._lock:
            return self.last_snapshot if self.last_snapshot else None

    def check_nodes_in_running(self, nodes: List[str]) -> Dict[str, bool]:
        with self._lock:
            running_set = set(self._node_pool.running_nodes)
            return {node: (node in running_set) for node in nodes}

    def add_fault_nodes(self, fault_nodes: List[str], fault_reason: Dict = None):
        """
        手动标记故障节点（持有 self._lock → 阻止 NHCMonitor 运行）

        Args:
            fault_nodes: 故障节点列表
            fault_reason: 故障原因描述（可选）
                payload 结构示例:
                {
                    "type": "loss",           # 外层类型
                    "data": {                 # 内层数据
                        "type": "loss_nan_inf",  # 内层类型（如 loss_nan_inf, grad_too_large 等）
                        "rank": 0,
                        "loss": nan,
                        "grad_norm": 1.0,
                        "message": "iter 100: Loss为NaN"
                    },
                    "cur_iter": 100,
                    "timestamp": "2024-01-01 12:00:00"
                }
                - 如果传入，直接用于 bl_mgr.report_fault
                - 如果不传，则通过 NHC 检测获取故障详情
        """
        with self._lock:
            logger.error(f"[Fault Mark] Nodes to check: {fault_nodes}")
            fault_node_reasons = {node: "Fault reported" for node in fault_nodes}
            try:
                if fault_reason:
                    # 直接使用传入的故障原因
                    logger.error(f"[Fault Mark] Using provided fault_reason: {fault_reason}")
                    # 从 payload 中提取信息
                    # fault_reason 结构: {"type": "loss", "data": {...}, "cur_iter": 100, "timestamp": ...}
                    data = fault_reason.get("data", {})
                    inner_type = data.get("type", "unknown")      # 如 loss_nan_inf, grad_too_large
                    message = data.get("message", inner_type)     # 故障描述
                    cur_iter = fault_reason.get("cur_iter")       # 当前迭代
                    node_name = ','.join(fault_nodes)
                    self.bl_mgr.report_fault(
                        node_name,
                        fault_type=FaultType.GRADIENT_INF,  # loss/inf 类型故障使用 GRADIENT_INF
                        description=message,
                        micro_step=cur_iter
                    )
                    # 构建故障原因字典用于 snapshot
                    fault_node_reasons = {node: message for node in fault_nodes}
                    logger.info(f'>= fault_node_reasons:{fault_node_reasons}')
                else:
                    # 通过 NHC 检测获取故障详情
                    fault_node_reasons.update(self.detector.run_nhc_nodes(fault_nodes))
                    logger.error(f"[Fault Mark] NHC detection result: {fault_node_reasons}")
                    for node_name, log_content in fault_node_reasons.items():
                        logger.error(f'>= log_content:{log_content}')
                        fault_info = self.detector.parse_fault_log(log_content)
                        logger.error(f'>= fault_info:{fault_info}')

                        # 处理 fault_type 为 None 的情况
                        fault_type = fault_info['fault_type']
                        if fault_type is None:
                            logger.warning(f"[Fault Mark] Unable to parse fault type for node {node_name}, using default HCU fault type")
                            fault_type = FaultType.NETWORK
                            fault_info['description'] = log_content

                        self.bl_mgr.report_fault(
                            node_name,
                            fault_type,
                            fault_info['description'],
                            fault_info['error_code'],
                            fault_info['gpu_id']
                        )
            except Exception as e:
                logger.exception(f'>= 报错{e} <=')
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._check_manager._update_node_status(fault_nodes, "abnormal", now, source="manual_fault")
            launched = list(self._node_pool.running_nodes)
            self._node_pool.add_abnormal_nodes(fault_nodes)
            logger.error(f"[Fault Mark] Node status updated to 'abnormal': {fault_nodes}")
            if self.current_snapshot:
                if launched:
                    self.current_snapshot.running_nodes = launched
                self.current_snapshot.fault_nodes.update(fault_node_reasons)
                self.current_snapshot.fault_count += 1
                self._persist_current_snapshot()

    def add_fault_nodes_no(self, fault_nodes_no, fault_reason: Dict = None):
        """
        根据节点编号添加故障节点

        Args:
            fault_nodes_no: 节点编号（在 running_nodes 中的索引）
            fault_reason: 故障原因描述（可选）
        """
        if fault_nodes_no is None:
            return
        with self._lock:
            # Quarantining a running node clears the live list. More rank errors
            # from this attempt must still resolve against the original launch.
            running = self._node_pool.running_nodes
            if not running and self.current_snapshot:
                running = self.current_snapshot.running_nodes
            if isinstance(fault_nodes_no, int) and 0 <= fault_nodes_no < len(running):
                return self.add_fault_nodes([running[fault_nodes_no]], fault_reason=fault_reason)
        return False

    def add_current_snapshot_param(self, **kwargs: Any):
        with self._lock:
            if self.current_snapshot:
                for param_name, param_value in kwargs.items():
                    setattr(self.current_snapshot, param_name, param_value)
                self._persist_current_snapshot()

    def reset_node_pool(
        self,
        total_nodes: list[str],
        abnormal_nodes: list[str] | None = None,
    ) -> None:
        with self._lock:
            self._node_pool.reset_node_pool(total_nodes, abnormal_nodes)

    def stop_monitor(self):
        self._check_manager.stop()

    def start_monitor(self):
        self._check_manager.start()

    def __getattr__(self, name):
        """透传原 NodePool 的所有其他接口"""
        return getattr(self._node_pool, name)
