#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：clush 在宿主机调用的唯一入口：确认本机身份，进入目标环境，打包结果。
# 用法：entry.sh check|node|group|container
#   check   全部节点：空闲门禁 + 目标环境预检（node_check.sh）
#   node    全部节点：idle-check / platform / checkdeepep（node_run.sh）
#   group   组首节点：组内依次执行全部测试项（group_run.sh）
#   container 每节点：按既有阶段表执行完整容器操作（container.sh）

set -uo pipefail
umask 022
PAYLOAD=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${PAYLOAD}/../config.env"
source "${PAYLOAD}/common.sh"

MODE=${1:-}

# ===================================== Identity =====================================
# hostfile 可写主机名或 IP；同一台机器对上多行也判失败。
self_node() {
    local names line
    local -a matches=()
    names=" $(hostname) $(hostname -s) $(hostname -f 2>/dev/null) $(hostname -I 2>/dev/null) "
    while IFS= read -r line; do
        [[ -n ${line} && ${names} == *" ${line} "* ]] && matches+=("${line}")
    done < "${HCU_RUN_DIR}/plan/all.hosts"
    (( ${#matches[@]} == 1 )) || return 1
    echo "${matches[0]}"
}

finish() {
    tar -czf "${HCU_RUN_DIR}/out.tgz" -C "${HCU_RUN_DIR}" results
    exit "$1"
}

fail_finish() {
    hcu_status node.status FAIL "$1" "$2" "$3"
    finish "$2"
}

# ===================================== Target =====================================
is_container() { [[ ${HCU_SCENARIO} == per-node-container ]]; }

check_container() {
    local actual running image expected
    actual=$(docker inspect --type container -f '{{.State.Running}}|{{.Image}}' "${HCU_CONTAINER}" 2>&1) \
        || fail_finish CONTAINER_NOT_READY 69 "容器不可用：${actual}"
    IFS='|' read -r running image <<< "${actual}"
    [[ ${running} == true ]] || fail_finish CONTAINER_NOT_READY 69 "容器未运行：${HCU_CONTAINER}"
    expected=$(docker image inspect -f '{{.Id}}' "${HCU_IMAGE}" 2>&1) \
        || fail_finish IMAGE_MISMATCH 69 "镜像不可用：${expected}"
    [[ ${image} == "${expected}" ]] || fail_finish IMAGE_MISMATCH 69 "容器实际镜像与请求镜像不一致"
    printf '%s\n' "${image}" > "${RESULTS}/image.id"
    docker_exec=(docker exec -e "HCU_NODE=${NODE}" -e "HCU_TARGET=${TARGET}")
    [[ -z ${HCU_CONTAINER_USER} ]] || docker_exec+=(--user "${HCU_CONTAINER_USER}")
    docker_exec+=("${HCU_CONTAINER}")
    "${docker_exec[@]}" test -r "${HCU_RUN_DIR}/RUN_ID" \
        || fail_finish WORKDIR_NOT_MOUNTED 69 "容器内不可见 ${HCU_RUN_DIR}；请用 container-create 重建或挂载 remote-workdir"
    docker inspect --type container -f '{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}' "${HCU_CONTAINER}" \
        > "${RESULTS}/container.capability" 2>/dev/null || echo unknown > "${RESULTS}/container.capability"
}

# 输出直接写日志，不用 tee：千节点的输出不回流到 clush 终端。
run_target() {
    local script=$1 limit=$2 log=$3
    if is_container; then
        "${docker_exec[@]}" timeout --signal=TERM --kill-after=30s "${limit}" \
            bash "${PAYLOAD}/${script}" >> "${log}" 2>&1
    else
        HCU_NODE=${NODE} HCU_TARGET=${TARGET} timeout --signal=TERM --kill-after=30s "${limit}" \
            bash "${PAYLOAD}/${script}" >> "${log}" 2>&1
    fi
}

# ===================================== Execution =====================================
rm -f "${HCU_RUN_DIR}/out.tgz"
NODE=$(self_node) || fail_finish NODE_IDENTITY_FAILED 69 "本机 $(hostname) 未能唯一匹配 hostfile 中的一行"
TARGET=$(awk -F'\t' -v node="${NODE}" '$2 == node {print $1}' "${HCU_RUN_DIR}/plan/targets.tsv")

case "${MODE}" in
    check)
        rm -f "${RESULTS}/node.status"
        if (( ! HCU_SKIP_IDLE )); then
            bash "${PAYLOAD}/idle.sh" gate > "${RESULTS}/idle.log" 2>&1
            case $? in
                0) ;;
                2) fail_finish HOST_NOT_IDLE 68 "宿主机存在占卡" ;;
                *) fail_finish IDLE_EVIDENCE_INVALID 68 "空闲证据无效，详见 idle.log" ;;
            esac
        fi
        is_container && check_container
        run_target node_check.sh "${HCU_CHECK_TIMEOUT}" "${RESULTS}/node.log"
        code=$?
        [[ -s ${RESULTS}/node.status ]] || hcu_status node.status FAIL NODE_CHECK_FAILED "${code}" "预检未完成，详见 node.log"
        ;;
    node)
        rm -f "${RESULTS}/node.status"
        if [[ ${HCU_OPERATION} == idle-check ]]; then
            bash "${PAYLOAD}/idle.sh" run > "${RESULTS}/idle.log" 2>&1
            code=$?
            hcu_status node.status "$( (( code )) && echo FAIL || echo PASS )" IDLE_DONE "${code}" "详见 idle.log"
        else
            is_container && check_container
            run_target node_run.sh "${HCU_TEST_TIMEOUT}" "${RESULTS}/node.log"
            code=$?
            [[ -s ${RESULTS}/node.status ]] || hcu_status node.status FAIL "$( (( code == 124 )) && echo TASK_TIMEOUT || echo NODE_RUN_FAILED )" "${code}" "执行未完成，详见 node.log"
        fi
        ;;
    group)
        is_container && check_container
        if is_container; then
            "${docker_exec[@]}" bash "${PAYLOAD}/group_run.sh" >> "${RESULTS}/group.log" 2>&1
        else
            HCU_NODE=${NODE} HCU_TARGET=${TARGET} bash "${PAYLOAD}/group_run.sh" >> "${RESULTS}/group.log" 2>&1
        fi
        ;;
    container)
        rm -f "${RESULTS}/node.status"
        HCU_NODE=${NODE} HCU_TARGET=${TARGET} bash "${PAYLOAD}/container.sh" > "${RESULTS}/node.log" 2>&1
        code=$?
        [[ -s ${RESULTS}/node.status ]] || hcu_status node.status FAIL CONTAINER_RUN_FAILED "${code:-1}" "容器操作未完成，详见 node.log"
        ;;
    *)
        echo "ERROR: unknown mode '${MODE}'. Use check, node, group or container." >&2
        exit 64
        ;;
esac
finish 0
