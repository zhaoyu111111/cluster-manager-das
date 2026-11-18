#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：情况1的目标环境入口：每节点执行 platform 或 checkdeepep，写节点状态。
# 职责边界：只采集和检查；健康规则与节点一致性由控制端报告判定。

set -uo pipefail
PAYLOAD=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${PAYLOAD}/../config.env"
source "${PAYLOAD}/common.sh"

TOKEN=${HCU_RUN_ID}_${HCU_TARGET}_${HCU_OPERATION}

hcu_source_env || hcu_die ENV_SCRIPT_FAILED 69 "环境脚本加载失败：${HCU_ENV_SCRIPT}"

case "${HCU_OPERATION}" in
    platform)
        command -v "${HCU_PYTHON}" >/dev/null || hcu_die TARGET_PYTHON_MISSING 69 "目标 Python 不存在：${HCU_PYTHON}"
        printf '[PLATFORM-BEGIN] token=%s\n' "${TOKEN}"
        "${HCU_PYTHON}" -B "${PAYLOAD}/platform_probe.py"
        code=$?
        printf '[PLATFORM-END] token=%s exit_code=%s\n' "${TOKEN}" "${code}"
        if (( code )); then
            hcu_status node.status FAIL PLATFORM_EXECUTION_FAILED "${code}" "采集失败，详见日志"
        else
            hcu_status node.status PASS PLATFORM_COLLECTED 0 "平台采集完成，健康和节点一致性由最终报告判定"
        fi
        ;;
    checkdeepep)
        bash "${PAYLOAD}/check_deepep_env.sh"
        code=$?
        case ${code} in
            0) hcu_status node.status PASS CHECK_OK 0 "原子环境检查通过" ;;
            1) hcu_status node.status FAIL CHECK_FAILED 1 "原子环境检查包含 FAIL" ;;
            2) hcu_status node.status WARN CHECK_WARN 2 "原子环境检查包含 WARN" ;;
            *) hcu_status node.status FAIL CHECK_EXECUTION_FAILED "${code}" "原始脚本异常退出" ;;
        esac
        ;;
    *)
        hcu_die UNKNOWN_OPERATION 64 "未知操作：${HCU_OPERATION}"
        ;;
esac
