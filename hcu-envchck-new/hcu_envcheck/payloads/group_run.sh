#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：情况2的组入口，在组首节点目标环境内执行：核对组员后依次跑全部测试项。
# 职责边界：RCCL 单项执行失败后停止本组后续项，保留原始退出码；日志写入本组文件。

set -uo pipefail
PAYLOAD=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${PAYLOAD}/../config.env"
source "${PAYLOAD}/common.sh"

GROUP=${HCU_TARGET}
HOSTFILE=${HCU_RUN_DIR}/plan/groups/${GROUP}/hostfile
LOG=${RESULTS}/${GROUP}.log
export HCU_GROUP=${GROUP}

group_fail() {
    echo "[ERROR] $3" >&2
    hcu_status group.status FAIL "$1" "$2" "$3"
    exit 0
}

: > "${LOG}"
rm -f "${RESULTS}/group.status"
hcu_source_env || group_fail ENV_SCRIPT_FAILED 69 "环境脚本加载失败：${HCU_ENV_SCRIPT}"

# ===================================== Group Peers =====================================
# mpirun 走节点间 SSH；核对 RUN_ID 确认进入的是本次任务所在环境。
EXPECTED=$(cat "${HCU_RUN_DIR}/RUN_ID")
NNODES=$(grep -c . "${HOSTFILE}")
if [[ ${HCU_OPERATION} == rccl ]] || (( NNODES > 1 )); then
    while IFS= read -r member; do
        observed=$(timeout 20s ssh -n -p "${HCU_PEER_PORT}" -o BatchMode=yes -o ConnectTimeout=5 \
            "${member}" cat "${HCU_RUN_DIR}/RUN_ID" 2>>"${LOG}") \
            || group_fail PEER_VERIFICATION_FAILED 69 "组内 SSH 失败 ${member}:${HCU_PEER_PORT}：${observed}"
        [[ ${observed} == "${EXPECTED}" ]] \
            || group_fail PEER_VERIFICATION_FAILED 69 "${member} 不是本次任务环境"
    done < "${HOSTFILE}"
fi

# ===================================== Tests =====================================
case "${HCU_OPERATION}" in
    rccl)      SCRIPT=rccl.sh ;;
    deepep)    SCRIPT=deepep.sh ;;
    byte-flux) SCRIPT=byte_flux.sh ;;
    *)         group_fail UNKNOWN_OPERATION 64 "未知操作：${HCU_OPERATION}" ;;
esac

for test in ${HCU_TESTS}; do
    token=${HCU_RUN_ID}_${GROUP}_${test}
    {
        printf '[TEST-BEGIN] test=%s token=%s\n' "${test}" "${token}"
        code=0
        timeout --signal=TERM --kill-after=30s "${HCU_TEST_TIMEOUT}" \
            bash "${PAYLOAD}/${SCRIPT}" "${test}" || code=$?
        printf '\n[TEST-END] test=%s token=%s exit_code=%s\n' "${test}" "${token}" "${code}"
    } >> "${LOG}" 2>&1
    if [[ ${HCU_OPERATION} == rccl ]] && (( code != 0 )); then
        break
    fi
done
