#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：情况2的节点预检，在目标环境（容器或 Conda）内执行，每节点一次。
# 职责边界：只读检查环境、卡数、依赖和测试程序；不启动性能测试。

set -euo pipefail
PAYLOAD=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${PAYLOAD}/../config.env"
source "${PAYLOAD}/common.sh"

NODE=${HCU_NODE}
PYTHON=${HCU_PYTHON}

# ===================================== Environment =====================================
hcu_source_env || hcu_die ENV_SCRIPT_FAILED 69 "环境脚本加载失败：${HCU_ENV_SCRIPT}"
command -v "${PYTHON}" >/dev/null || hcu_die TARGET_PYTHON_MISSING 69 "目标 Python 不存在：${PYTHON}"
printf '[ENV-SOURCE] node=%s pwd=%s script=%s\n' "${NODE}" "${PWD}" "${HCU_ENV_SCRIPT:-未指定}"

# ===================================== Dependency =====================================
case "${HCU_OPERATION}" in
    deepep)    LIBRARY=DeepEP ;;
    byte-flux) LIBRARY=flux ;;
    *)         LIBRARY='' ;;
esac
if [[ -n ${LIBRARY} ]]; then
    export HCU_DEPENDENCY_MANIFEST=${HCU_DEPENDENCY_MANIFEST:-}
    code=0
    "${PYTHON}" -B "${PAYLOAD}/dependency_check.py" \
        --source "${HCU_RUN_DIR}/source/${LIBRARY}/SOURCE_METADATA.json" || code=$?
    case ${code} in
        0)  ;;
        65) hcu_die DEPENDENCY_MISMATCH 65 "${LIBRARY} 实际模块与构建来源不一致" ;;
        66) hcu_die DEPENDENCY_MISSING 66 "${LIBRARY} 依赖缺失" ;;
        *)  hcu_die DEPENDENCY_UNKNOWN "${code}" "${LIBRARY} 构建来源无法确认" ;;
    esac
fi

# ===================================== GPU =====================================
RUNTIME=torch
[[ ${HCU_OPERATION} == rccl ]] && RUNTIME=hip
"${PYTHON}" -B "${PAYLOAD}/gpu_check.py" --count "${HCU_GPUS_PER_NODE}" --runtime "${RUNTIME}" \
    || hcu_die GPU_CHECK_FAILED 69 "可见卡数不足或运行时不可用"

# ===================================== Test Prerequisites =====================================
case "${HCU_OPERATION}" in
    rccl)
        hcu_mpirun >/dev/null || hcu_die MPIRUN_MISSING 69 "mpirun 不存在；请通过 env.sh 配置 PATH/MPI_HOME/MPIRUN_BIN"
        for item in ${HCU_TESTS}; do
            hcu_rccl_binary "${item}" >/dev/null || hcu_die RCCL_BINARY_MISSING 69 "RCCL 程序不可执行：${item}_perf"
        done
        ;;
    deepep)
        [[ ${HCU_DEEPEP_RUNNABLE_TESTS} == intranode ]] || hcu_mpirun >/dev/null \
            || hcu_die MPIRUN_MISSING 69 "mpirun 不存在；请通过 env.sh 配置 PATH/MPI_HOME/MPIRUN_BIN"
        "${PYTHON}" -B -c 'import torch, deep_ep; print("[DEEPEP-DEPENDENCY] torch=%s module=%s" % (torch.__version__, deep_ep.__file__))' \
            || hcu_die DEPENDENCY_MISSING 66 "无法导入 torch 或 deep_ep"
        code=0
        bash "${PAYLOAD}/check_deepep_env.sh" || code=$?
        printf '__HCU_DEEPEP_ENV__\t%s\t%s\n' "${NODE}" "${code}"
        case ${code} in
            0|2) ;;
            *)   hcu_die CHECK_FAILED "${code}" "DeepEP 环境检查未通过" ;;
        esac
        ;;
esac

hcu_status node.status PASS PREPARE_READY 0 "预检通过"
