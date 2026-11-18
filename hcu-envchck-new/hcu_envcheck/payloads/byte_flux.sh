#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：单节点 torchrun 执行一个 byte-flux AG 项，TP 规模取 HCU_GPUS_PER_NODE。
# 用法：byte_flux.sh tpN（N 与 --gpus-per-node 相同，由 group_run.sh 调用）

set -euo pipefail
CURRENT_DIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${CURRENT_DIR}/../config.env"

TEST=${1:-}

# ===================================== Test Config =====================================
GPUS=${HCU_GPUS_PER_NODE}
M=2048
N=10240
K=8192
ITERS=10
WARMUP=5
PYTHON=${HCU_PYTHON}
SCRIPT=${HCU_RUN_DIR}/source/flux/test/python/test_ag_kernel.py

[[ ${TEST} == "tp${GPUS}" ]] || { echo "ERROR: expected tp${GPUS}, got '${TEST}'."; exit 64; }
# 使用目标运行时可见的前 N 张卡；保留 env.sh 设置的设备可见性和顺序。
DEVICES=0
for (( device = 1; device < GPUS; device++ )); do DEVICES+=,${device}; done

TORCHRUN_ARGS=(
    --standalone
    --nnodes=1
    --nproc-per-node=${GPUS}
)
TEST_ARGS=(
    ${SCRIPT}
    ${M} ${N} ${K}
    --dtype=float16
    --iters=${ITERS}
    --warmup=${WARMUP}
)

# ===================================== Summary =====================================
echo "======================================"
echo "byte-flux:   ${TEST}"
echo "Devices (visible indices): ${DEVICES}"
echo "Shape:       M=${M} N=${N} K=${K}"
echo "======================================"
printf '[BYTE-FLUX-LAUNCH] {"node": "%s", "workers": %s, "devices": "%s", "m": %s, "n": %s, "k": %s, "dtype": "float16"}\n' \
    "${HCU_NODE}" "${GPUS}" "${DEVICES}" "${M}" "${N}" "${K}"

# ===================================== Execution =====================================
cd "${HCU_WORKDIR}"
exec "${PYTHON}" -B -m torch.distributed.run "${TORCHRUN_ARGS[@]}" "${TEST_ARGS[@]}"
