#!/usr/bin/env bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

# Slurm MPI. Pass --hostfile PATH or --supernodes [JSON].
export WORK_DIR="${WORK_DIR:-$(pwd)}"
export LOG_DIR="${LOG_DIR:-/path/to/logs}"
export CLUSTER_LAUNCH_MODE=mpi
export CLUSTER_SCHEDULE=SLURM
export LOG_PARSER_TYPE="${LOG_PARSER_TYPE:-base}"

export MEGATRON_SCRIPT_PATH="/path/to/train.sh"
NODES_NUM="${NODES_NUM:-4}"
SLOTS="${SLOTS:-8}"
RUN_PATH="/path/to/run.sh"
JOB_ID="1234"
JOB_NAME="slurm_example"
SBATCH_SCRIPT="/path/to/sbatch.sh"

python /path/to/cluster-manager-das/hcu-training-reliability/cluster_manager/main.py \
  --nodes_num "${NODES_NUM}" \
  --slots "${SLOTS}" \
  --exec "${RUN_PATH}" \
  --job_id "${JOB_ID}" \
  --job_name "${JOB_NAME}" \
  --sbatch_script "${SBATCH_SCRIPT}" \
  "$@"
