#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：payload 公共函数：状态文件、环境加载、MPI 定位和 RCCL 程序定位。
# 职责边界：只被 source；不解析指标、不判定性能。调用方先 source config.env。

RESULTS=${HCU_RUN_DIR}/results

# 状态文件一行四列：status reason exit_code detail，控制端据此生成执行事实。
hcu_status() {
    local name=$1 status=$2 reason=$3 code=$4 detail=${5//$'\t'/ }
    printf '%s\t%s\t%s\t%s\n' "${status}" "${reason}" "${code}" "${detail//$'\n'/ }" > "${RESULTS}/${name}"
}

hcu_die() {
    echo "[ERROR] $3" >&2
    hcu_status node.status FAIL "$1" "$2" "$3"
    exit "$2"
}

# env.sh 不保证兼容 set -u，加载期间临时放开；set -a 让未 export 的配置进入子脚本。
hcu_source_env() {
    cd "${HCU_WORKDIR}" || return 69
    [[ -n ${HCU_ENV_SCRIPT} ]] || return 0
    set +u
    set -a
    source "${HCU_ENV_SCRIPT}"
    local code=$?
    set +a
    set -u
    return ${code}
}

hcu_mpirun() {
    local mpi_bin=${MPIRUN_BIN:-${MPI_BIN:-}} root
    [[ -n ${mpi_bin} ]] || mpi_bin=$(command -v mpirun 2>/dev/null) || mpi_bin=''
    if [[ -z ${mpi_bin} ]]; then
        for root in "${MPI_HOME:-}" "${MPI_ROOT:-}" "${OPENMPI_HOME:-}" "${OMPI_HOME:-}"; do
            [[ -n ${root} && -x ${root}/bin/mpirun ]] && mpi_bin=${root}/bin/mpirun && break
        done
    fi
    command -v "${mpi_bin}" >/dev/null || return 69
    echo "${mpi_bin}"
}

# 显式目录优先，其次 PATH，再按环境搜索根查找。
hcu_rccl_binary() {
    local item=$1 bin='' root candidate
    local bin_dir=${RCCL_BIN_DIR:-${RCCL_TESTS_BIN_DIR:-}}
    [[ -n ${bin_dir} ]] && bin=${bin_dir}/${item}_perf
    [[ -n ${bin} ]] || bin=$(command -v "${item}_perf" 2>/dev/null) || bin=''
    if [[ -z ${bin} ]]; then
        for root in "${RCCL_TESTS_PATH:-}" "${RCCL_TESTS_HOME:-}" "${RCCL_HOME:-}" "${RCCL_PATH:-}" \
                    "${DTK_ROOT:-}" "${DTK_HOME:-}" "${DTK_PATH:-}" "${ROCM_PATH:-}" "${ROCM_HOME:-}"; do
            [[ -n ${root} ]] || continue
            for candidate in "${root}/${item}_perf" "${root}/bin/${item}_perf" "${root}/build/${item}_perf" \
                             "${root}/rccl-test/build/${item}_perf" "${root}/rccl-tests/build/${item}_perf"; do
                [[ -x ${candidate} ]] && bin=${candidate} && break
            done
            [[ -n ${bin} ]] && break
        done
    fi
    [[ -n ${bin} ]] || bin=/opt/rccl-test/build/${item}_perf
    [[ -x ${bin} ]] || return 69
    echo "${bin}"
}

# 组装 mpirun 环境转发：固定名单 + 通信调优前缀的已导出项。
hcu_env_args() {
    local name
    env_args=()
    for name in PATH LD_LIBRARY_PATH LD_PRELOAD PYTHONPATH ROCM_PATH ROCM_HOME HIP_PATH \
                DTK_ROOT DTK_HOME DTK_PATH MASTER_ADDR MASTER_PORT; do
        [[ -n ${!name:-} ]] && env_args+=(-x "${name}")
    done
    while IFS= read -r name; do
        case ${name} in
            RCCL_BASELINE*|RCCL_MARGIN) continue ;;
            NCCL_*|RCCL_*|UCX_*|ROCSHMEM_*|DEEPEP_*|GLOO_*|OMPI_MCA_*) env_args+=(-x "${name}") ;;
        esac
    done < <(compgen -e | LC_ALL=C sort -u)
}
