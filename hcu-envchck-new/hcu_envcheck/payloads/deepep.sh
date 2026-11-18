#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：执行一个 DeepEP 测试项：多机 mpirun -np 节点数（slots=1），每节点启动一个 torchrun。
# 用法：deepep.sh intranode|lowl|norm（由 group_run.sh 在已加载环境的组首节点调用）

set -euo pipefail
CURRENT_DIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${CURRENT_DIR}/../config.env"
source "${CURRENT_DIR}/common.sh"

MODE=${1:-}

# ===================================== Multi-node Topology =====================================
HOSTFILE_SLOTS=${HCU_RUN_DIR}/plan/groups/${HCU_GROUP}/slotsfile
NNODES=$(grep -c . "${HOSTFILE_SLOTS}")
GPUS=${HCU_GPUS_PER_NODE}
PORT=${HCU_PEER_PORT}
PYTHON=${HCU_PYTHON}
# First host is the rendezvous master; 各组首节点不同，固定端口不会互相冲突。
MASTER_ADDR=$(awk 'NR == 1 {print $1}' "${HOSTFILE_SLOTS}")
MASTER_PORT=29501
export MASTER_ADDR MASTER_PORT

# ===================================== Test Config =====================================
TEST_DIR=${HCU_RUN_DIR}/source/DeepEP/tests
case "${MODE}" in
    intranode) TEST_FILE=${TEST_DIR}/test_intranode.py ;;
    lowl)      TEST_FILE=${TEST_DIR}/test_low_latency.py ;;
    norm)      TEST_FILE=${TEST_DIR}/test_internode.py ;;
    *)         echo "ERROR: unknown mode '${MODE}'."; exit 64 ;;
esac
unsupported_key=HCU_DEEPEP_UNSUPPORTED_${MODE^^}
if [[ -n ${!unsupported_key:-} ]]; then
    printf '[DEEPEP-UNSUPPORTED] %s\n' "${!unsupported_key}" >&2
    exit 64
fi
TEST_ARGS=(
    ${TEST_FILE}
    --num-processes ${GPUS}
)

# ===================================== Summary =====================================
echo "======================================"
echo "DeepEP:      ${MODE}"
echo "Nodes:       ${NNODES}"
echo "GPUs:        ${GPUS}/node"
echo "Master:      ${MASTER_ADDR}:${MASTER_PORT}"
echo "======================================"

# ===================================== Execution =====================================
cd "${TEST_DIR}"

if [[ ${MODE} == intranode ]]; then
    # 单机模式不经过 MPI/torchrun，测试文件自行创建 GPU workers。
    printf '[DEEPEP-LAUNCH] node=%s nnodes=1 node_rank=0 gpu_workers=%s mode=%s\n' "${HCU_NODE}" "${GPUS}" "${MODE}"
    unset RANK WORLD_SIZE LOCAL_RANK LOCAL_WORLD_SIZE
    exec "${PYTHON}" "${TEST_ARGS[@]}"
fi

hcu_env_args

TORCHRUN_ARGS=(
    --nproc-per-node=1
    --nnodes=${NNODES}
    --master-addr=${MASTER_ADDR}
    --master-port=${MASTER_PORT}
)

# One mpirun process per node (slots=1); each starts its own torchrun, and the
# test file spawns that node's GPU workers. The remote command is assembled as
# a string because --node-rank must stay literal until it is evaluated on the
# remote host, where mpirun sets OMPI_COMM_WORLD_RANK.
TORCHRUN_QUOTED="$(printf '%q ' "${TORCHRUN_ARGS[@]}")"
TEST_QUOTED="$(printf '%q ' "${TEST_ARGS[@]}")"
ENV_SOURCE=""
[[ -z ${HCU_ENV_SCRIPT} ]] || ENV_SOURCE="source $(printf '%q' "${HCU_ENV_SCRIPT}") && "
LAUNCH_LOG="printf '[DEEPEP-LAUNCH] node=%s nnodes=${NNODES} node_rank=%s gpu_workers=${GPUS} mode=${MODE}\n' \
\"\$(awk -v row=\$((OMPI_COMM_WORLD_RANK + 1)) 'NR == row {print \$1}' $(printf '%q' "${HOSTFILE_SLOTS}"))\" \"\${OMPI_COMM_WORLD_RANK}\""
REMOTE_CMD="$(printf '%q' "${PYTHON}") -m torch.distributed.run --node-rank=\${OMPI_COMM_WORLD_RANK} ${TORCHRUN_QUOTED} ${TEST_QUOTED}"

"$(hcu_mpirun)" -np "${NNODES}" --hostfile "${HOSTFILE_SLOTS}" \
    --allow-run-as-root \
    --bind-to none \
    --mca plm_rsh_no_tree_spawn 1 \
    --mca plm_rsh_args "-p ${PORT} -o BatchMode=yes" \
    "${env_args[@]}" \
    bash -c "
    ${ENV_SOURCE}
    cd $(printf '%q' "${TEST_DIR}") && \
    ${LAUNCH_LOG} && \
    ${REMOTE_CMD}"
