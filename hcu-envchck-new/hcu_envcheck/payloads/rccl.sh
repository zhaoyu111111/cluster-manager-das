#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：执行一个 RCCL 测试项：mpirun -np 节点数×卡数，直接启动 RCCL 测试程序。
# 用法：rccl.sh TEST（由 group_run.sh 在已加载环境的组首节点调用）

set -euo pipefail
CURRENT_DIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${CURRENT_DIR}/../config.env"
source "${CURRENT_DIR}/common.sh"

TEST=${1:-}

# ===================================== Multi-node Topology =====================================
HOSTFILE_SLOTS=${HCU_RUN_DIR}/plan/groups/${HCU_GROUP}/slotsfile
NNODES=$(grep -c . "${HOSTFILE_SLOTS}")
GPUS=${HCU_GPUS_PER_NODE}
WORLD_GPUS=$((NNODES * GPUS))
PORT=${HCU_PEER_PORT}

# ===================================== Test Config =====================================
MPIRUN=$(hcu_mpirun)
BINARY=$(hcu_rccl_binary "${TEST}")
RCCL_ARGS=(
    -g 1
    -b "${RCCL_ARG_B:-4}"
    -e "${RCCL_ARG_E:-1G}"
    -f "${RCCL_ARG_F:-2}"
    -n "${RCCL_ARG_N:-20}"
    -w "${RCCL_ARG_W:-5}"
)

# ===================================== Summary =====================================
echo "======================================"
echo "RCCL:        ${TEST}"
echo "Nodes:       ${NNODES}"
echo "GPUs:        ${GPUS}/node, ${WORLD_GPUS} total"
echo "Binary:      ${BINARY}"
echo "======================================"

# ===================================== Execution =====================================
hcu_env_args

ENV_SOURCE=""
[[ -z ${HCU_ENV_SCRIPT} ]] || ENV_SOURCE="source $(printf '%q' "${HCU_ENV_SCRIPT}") && "
REMOTE_CMD="${ENV_SOURCE}cd $(printf '%q' "${HCU_WORKDIR}") && exec $(printf '%q ' "${BINARY}" "${RCCL_ARGS[@]}")"

"${MPIRUN}" -np "${WORLD_GPUS}" --hostfile "${HOSTFILE_SLOTS}" \
    --allow-run-as-root \
    --bind-to none \
    --mca plm_rsh_no_tree_spawn 1 \
    --mca plm_rsh_args "-p ${PORT} -o BatchMode=yes" \
    "${env_args[@]}" \
    bash -c "${REMOTE_CMD}"
