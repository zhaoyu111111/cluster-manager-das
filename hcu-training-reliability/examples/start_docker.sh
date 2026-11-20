#!/usr/bin/env bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

# Check the source before nohup so conflicting arguments fail in the calling shell.
hostfile_requested=false
supernodes_requested=false
cli_args=("$@")
for i in "${!cli_args[@]}"; do
    case "${cli_args[i]}" in
        --hostfile)
            hostfile_requested=true
            hostfile_path="${cli_args[i+1]:-}"
            ;;
        --hostfile=*)
            hostfile_requested=true
            hostfile_path="${cli_args[i]#*=}"
            ;;
        --supernodes|--supernodes=*|--supernodes-config|--supernodes-config=*)
            supernodes_requested=true
            ;;
    esac
done
if [[ "${hostfile_requested}" == "${supernodes_requested}" ]]; then
    echo "Pass exactly one of --hostfile PATH or --supernodes [JSON]." >&2
    exit 2
fi
if [[ "${hostfile_requested}" == true && -v SUPERNODES_CONFIG ]]; then
    echo "Unset SUPERNODES_CONFIG to use --hostfile." >&2
    exit 2
fi
if [[ "${hostfile_requested}" == true && -z "${hostfile_path:-}" ]]; then
    echo "--hostfile requires a path." >&2
    exit 2
fi
if [[ "${hostfile_requested}" == true && ! -r "${hostfile_path:-}" ]]; then
    echo "Hostfile not found or unreadable: ${hostfile_path}" >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${WORK_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
LOG_DIR="${LOG_DIR:-${WORK_DIR}/logs}"

export WORK_DIR
export LOG_DIR
export CLUSTER_LAUNCH_MODE="${CLUSTER_LAUNCH_MODE:-docker}"
export CLUSTER_SCHEDULE=NONE
export LOG_PARSER_TYPE="${LOG_PARSER_TYPE:-base}"
export MPI_LAUNCH_TIMEOUT="${MPI_LAUNCH_TIMEOUT:-300}"
export MPI_HOST_SSH_PORT="${MPI_HOST_SSH_PORT:-22}"
# 准备容器时自动生成独立密钥、交换公钥并验证免密；训练脚本应使用相同端口。
export DOCKER_CONTAINER_SSH_PORT="${DOCKER_CONTAINER_SSH_PORT:-36000}"
export MPIRUN_ALLOW_RUN_AS_ROOT="${MPIRUN_ALLOW_RUN_AS_ROOT:-1}"

export DOCKER_CONTAINER_NAME="${DOCKER_CONTAINER_NAME:-cluster-manager}"
export RUN_NHC_EXEC_MODE="${RUN_NHC_EXEC_MODE:-host}"
export DOCKER_IMAGE="${DOCKER_IMAGE:?Set DOCKER_IMAGE to the training image}"
export DOCKER_HOST_SHARE_ROOT="${DOCKER_HOST_SHARE_ROOT:-}"
export DOCKER_CONTAINER_SHARE_ROOT="${DOCKER_CONTAINER_SHARE_ROOT:-}"
export DOCKER_REUSE_CONTAINER="${DOCKER_REUSE_CONTAINER:-1}"
export DOCKER_REMOVE_CONTAINER_ON_STOP="${DOCKER_REMOVE_CONTAINER_ON_STOP:-1}"
export MPI_FORWARD_ENV="${MPI_FORWARD_ENV:-LD_LIBRARY_PATH}"
export PYTHON_BIN="${PYTHON_BIN:-python3}"

export MEGATRON_SCRIPT_PATH="${MEGATRON_SCRIPT_PATH:-}"
SLOTS="${SLOTS:-8}"
# 训练容器内每节点 MPI 进程数，默认等于 SLOTS，适用于 MPI 直接启动各训练进程。
# mpirun 每节点启动一个 torchrun 时必须改为 1：
# 启动前 export MPI_RANKS_PER_NODE=1
# 仅用于 MPI/prterun 退出日志的故障节点映射，不控制训练节点数或 GPU 数。
export MPI_RANKS_PER_NODE="${MPI_RANKS_PER_NODE:-${SLOTS}}"
RUN_PATH="${RUN_PATH:-${DOCKER_EXEC_PATH:-/workspace/train/run.sh}}"
# Number of training nodes, excluding standby nodes.
NODES_NUM="${NODES_NUM:-1}"

mkdir -p "${LOG_DIR}"
LOG_DIR="$(cd -- "${LOG_DIR}" && pwd)"
export LOG_DIR
# Keep the caller's working directory for relative hostfile and JSON paths.
CLUSTER_MANAGER_MAIN="${CLUSTER_MANAGER_MAIN:-cluster_manager/main.py}"
if [[ "${CLUSTER_MANAGER_MAIN}" != /* ]]; then
    CLUSTER_MANAGER_MAIN="${SCRIPT_DIR}/../${CLUSTER_MANAGER_MAIN}"
fi
if [[ ! -f "${CLUSTER_MANAGER_MAIN}" ]]; then
    echo "Cluster manager entrypoint not found: ${CLUSTER_MANAGER_MAIN}" >&2
    exit 2
fi
nohup "${PYTHON_BIN}" "${CLUSTER_MANAGER_MAIN}" \
  --nodes_num "${NODES_NUM}" \
  --slots "${SLOTS}" \
  --mpi-ranks-per-node "${MPI_RANKS_PER_NODE}" \
  --exec "${RUN_PATH}" \
  "$@" >> "${LOG_DIR}/cluster_manager.log" 2>&1 &
